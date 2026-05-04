// Front Door Premium + WAF + Private Link origin -> ACA env.
// Critical: a Rule Set strips identifying response headers Front Door / origin add,
// so F12 in the user's browser sees only generic headers.
@description('Naming prefix.')
param namePrefix string
@description('Tags.')
param tags object
@description('FQDN of the broker container app on the ACA env load balancer (e.g. ca-cloak-broker.<env>.swedencentral.azurecontainerapps.io). Reachable only via the env Private Endpoint because vnetConfiguration.internal=true.')
param brokerFqdn string
@description('ACA managed environment resource ID (Private Link target).')
param brokerResourceId string
@description('Custom hostname (e.g. portal.contoso.com). Empty = use *.azurefd.net default.')
param portalHostname string

@description('Public source IP/CIDR allowlist for the WAF. Empty = no IP restriction.')
param allowedSourceIps array = []

var enforceIpAllowlist = !empty(allowedSourceIps)

// --- WAF policy ---
resource waf 'Microsoft.Network/FrontDoorWebApplicationFirewallPolicies@2024-02-01' = {
  name: 'waf${namePrefix}'
  location: 'Global'
  tags: tags
  sku: {
    name: 'Premium_AzureFrontDoor'
  }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: 'Prevention'
      // Disabled: WAF caps inspected body at 128 KB and 403s anything larger,
      // which breaks /upload (multipart up to upload_max_bytes). Body
      // validation (MIME allowlist, size caps, sha256) is enforced by the
      // broker itself in broker/app/upload.py.
      requestBodyCheck: 'Disabled'
    }
    managedRules: {
      managedRuleSets: [
        {
          ruleSetType: 'Microsoft_DefaultRuleSet'
          ruleSetVersion: '2.1'
          ruleSetAction: 'Block'
        }
        {
          ruleSetType: 'Microsoft_BotManagerRuleSet'
          ruleSetVersion: '1.1'
        }
      ]
    }
    customRules: {
      rules: enforceIpAllowlist ? [
        {
          name: 'AllowOnlyListedIps'
          priority: 100
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [
            {
              matchVariable: 'SocketAddr'
              operator: 'IPMatch'
              negateCondition: true
              matchValue: allowedSourceIps
            }
          ]
        }
      ] : []
    }
  }
}

// --- Front Door profile ---
resource profile 'Microsoft.Cdn/profiles@2024-02-01' = {
  name: 'afd-${namePrefix}'
  location: 'Global'
  tags: tags
  sku: {
    name: 'Premium_AzureFrontDoor'
  }
  identity: {
    type: 'SystemAssigned'
  }
}

resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2024-02-01' = {
  parent: profile
  name: 'ep-${namePrefix}'
  location: 'Global'
  tags: tags
  properties: {
    enabledState: 'Enabled'
  }
}

// --- Origin group + origin (Private Link to ACA env) ---
resource originGroup 'Microsoft.Cdn/profiles/originGroups@2024-02-01' = {
  parent: profile
  name: 'og-broker'
  properties: {
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 2
      additionalLatencyInMilliseconds: 50
    }
    healthProbeSettings: {
      probePath: '/healthz'
      probeRequestType: 'GET'
      probeProtocol: 'Https'
      probeIntervalInSeconds: 60
    }
    sessionAffinityState: 'Disabled'
  }
}

resource origin 'Microsoft.Cdn/profiles/originGroups/origins@2024-02-01' = {
  parent: originGroup
  name: 'broker'
  properties: {
    // brokerFqdn is the runtime ingress.fqdn of the broker container app:
    // '<app>.<env>.<region>.azurecontainerapps.io' (public-form, since the env
    // uses External VIP / internal=false). The env edge proxy uses Host header
    // matching to route from FD's PE -> this app.
    hostName: brokerFqdn
    httpPort: 80
    httpsPort: 443
    originHostHeader: brokerFqdn
    priority: 1
    weight: 1000
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
    sharedPrivateLinkResource: {
      privateLink: {
        id: brokerResourceId
      }
      groupId: 'managedEnvironments'
      privateLinkLocation: resourceGroup().location
      requestMessage: 'Front Door -> ACA env private link for ${namePrefix} broker'
    }
  }
}

// --- Rule Set: strip identifying response headers (Surface 1: F12 view) ---
resource ruleSet 'Microsoft.Cdn/profiles/ruleSets@2024-02-01' = {
  parent: profile
  name: 'StripIdentifyingHeaders'
}

// Strip Azure / Front Door fingerprint headers from responses to the user.
// Note: max 10 actions per rule, so we split strip + privacy headers into two rules.
resource stripRule 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = {
  parent: ruleSet
  name: 'stripHeaders'
  properties: {
    order: 1
    matchProcessingBehavior: 'Continue'
    conditions: []
    actions: [
      // Front Door rules engine forbids modifying these reserved response headers
      // (per https://learn.microsoft.com/azure/frontdoor/front-door-rules-engine-actions):
      //   Accept-Ranges, Host, Connection, Content-Length, Transfer-Encoding, TE,
      //   Last-Modified, Keep-Alive, Expect, Upgrade, If-*, Range, Warning, Forwarded,
      //   Via, X-Forwarded-*, X-Azure-RequestChain*, X-Azure-FDID, X-Azure-Ref,
      //   X-Ms-Via, X-Ms-Force-Refresh, X-MSEdge-Ref, plus any header prefixed `x-ec` or `x-fd`.
      // Those reserved headers (Via, X-Azure-Ref, X-MSEdge-Ref) WILL appear on responses
      // — they only reveal "behind some Azure CDN" with no tenant-specific info. Documented
      // as residual signals. We strip what is allowed AND actually leaks SaaS identity.
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'Server' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Request-Id' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Correlation-Id' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Powered-By' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Nextjs-Cache' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Nextjs-Prerender' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Delete', headerName: 'X-Nextjs-Stale-Time' } }
    ]
  }
}

resource privacyRule 'Microsoft.Cdn/profiles/ruleSets/rules@2024-02-01' = {
  parent: ruleSet
  name: 'privacyHeaders'
  properties: {
    order: 2
    matchProcessingBehavior: 'Continue'
    conditions: []
    actions: [
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Overwrite', headerName: 'Referrer-Policy', value: 'no-referrer' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Overwrite', headerName: 'X-Content-Type-Options', value: 'nosniff' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Overwrite', headerName: 'X-Frame-Options', value: 'SAMEORIGIN' } }
      { name: 'ModifyResponseHeader', parameters: { typeName: 'DeliveryRuleHeaderActionParameters', headerAction: 'Overwrite', headerName: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=(), interest-cohort=()' } }
    ]
  }
  dependsOn: [
    stripRule
  ]
}

// --- Security policy (binds WAF to endpoint) ---
resource securityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2024-02-01' = {
  parent: profile
  name: 'sp-${namePrefix}'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: {
        id: waf.id
      }
      associations: [
        {
          domains: [
            {
              id: endpoint.id
            }
          ]
          patternsToMatch: [ '/*' ]
        }
      ]
    }
  }
}

// --- Route ---
resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2024-02-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    originGroup: {
      id: originGroup.id
    }
    supportedProtocols: [ 'Http', 'Https' ]
    patternsToMatch: [ '/*' ]
    forwardingProtocol: 'HttpsOnly'
    linkToDefaultDomain: 'Enabled'
    httpsRedirect: 'Enabled'
    ruleSets: [
      {
        id: ruleSet.id
      }
    ]
    customDomains: empty(portalHostname) ? [] : [
      // NOTE: For a real custom domain you must:
      //   1) create a Microsoft.Cdn/profiles/customDomains resource
      //   2) validate via DNS TXT
      //   3) reference its id here.
      // Left as a TODO for the deploy script to wire after `portalHostname` is provided.
    ]
  }
  dependsOn: [
    origin
  ]
}

output endpointHostname string = endpoint.properties.hostName
output frontDoorProfileName string = profile.name
output wafName string = waf.name
