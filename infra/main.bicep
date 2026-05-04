// =====================================================================
// Azure SaaS Network-Identity Cloak — Path A
// Top-level Bicep (subscription scope)
// =====================================================================
//
// Deploys:
//   1. Resource group
//   2. Log Analytics workspace
//   3. Hub VNet (subnets: aca, sessions, dnsResolver, privateEndpoints)
//   4. NAT Gateway + Standard Public IP (egress to SaaS)
//   5. Key Vault (broker session-signing key, OIDC client secret)
//   6. DNS Private Resolver (outbound endpoint, region-consistent DNS)
//   7. ACA managed environment (workload profiles, internal ingress, VNet-injected)
//   8. ACA Container App: session broker (FastAPI)
//   9. ACA Dynamic Sessions custom-container pool (Kasm chromium sandboxes)
//  10. Private Endpoint from Front Door -> ACA env
//  11. Front Door Premium + WAF policy + route + response-header strip rule set
//
// Notes:
// - Region: swedencentral (Dynamic Sessions supported, EU residency, ~70ms RTT from IL)
// - The broker container image must already exist in ACR (or override `brokerImage` to a
//   placeholder image and update later). Same for the sandbox image.
// - External IdP (Auth0/Okta/Keycloak/Entra) is NOT provisioned here — broker reads
//   discovery URL + client ID from params, secret from Key Vault.
// =====================================================================

targetScope = 'subscription'

@description('Naming prefix for all resources. Lowercase letters/numbers, 2-10 chars.')
@minLength(2)
@maxLength(10)
param namePrefix string = 'cloak'

@description('Azure region for the hub. Must support ACA Dynamic Sessions.')
@allowed([
  'swedencentral'
  'westeurope'
  'northeurope'
  'eastus'
  'eastus2'
])
param location string = 'swedencentral'

@description('Tags applied to all resources.')
param tags object = {
  project: 'saas-network-identity-cloak'
  path: 'A'
  owner: 'platform'
}

@description('Address space for the hub VNet (CIDR /20 or larger).')
param vnetAddressSpace string = '10.80.0.0/20'

@description('Container image for the session broker (FastAPI). Build via scripts/build-and-push.sh and pin to a tag (or digest).')
param brokerImage string

@description('Container image for the per-browser sandbox (custom kiosk: Xvfb+x11vnc+websockify+noVNC+Chromium). Build via scripts/build-and-push.sh and pin to a tag (or digest).')
param sandboxImage string

@description('Target SaaS URL pinned per sandbox. Chromium kiosk loads --app=$SAAS_URL on start. Required.')
param saasUrl string

@description('Set to "1" to ignore TLS errors for $SAAS_URL inside the kiosk Chromium (PoC against self-signed origins). Set to "0" for CA-signed origins in production.')
@allowed([ '0', '1' ])
param insecureSaas string = '0'

@description('Name of the ACR (in same subscription) hosting the broker/sandbox images. Empty = images are public.')
param acrName string = ''

@description('External OIDC issuer URL (Auth0/Okta/Keycloak/Entra). Leave empty to use stub auth in PoC.')
param oidcIssuer string = ''

@description('External OIDC client ID. Leave empty to use stub auth in PoC.')
param oidcClientId string = ''

@description('Allowlist of user principal IDs / emails / sub claims that may use the cloak. Comma-separated.')
param userAllowlist string = ''

@description('Custom hostname for the public Front Door endpoint, e.g. portal.contoso.com. Empty = use default *.azurefd.net.')
param portalHostname string = ''

// ----- Resource group -----
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${namePrefix}-${location}'
  location: location
  tags: tags
}

// ----- Modules -----
module observability 'modules/observability.bicep' = {
  scope: rg
  name: 'observability'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module network 'modules/network.bicep' = {
  scope: rg
  name: 'network'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    vnetAddressSpace: vnetAddressSpace
  }
}

module keyvault 'modules/keyvault.bicep' = {
  scope: rg
  name: 'keyvault'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
  }
}

module dnsResolver 'modules/dns-resolver.bicep' = {
  scope: rg
  name: 'dns-resolver'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    vnetId: network.outputs.vnetId
    resolverSubnetId: network.outputs.dnsResolverSubnetId
  }
}

module acaEnv 'modules/aca-environment.bicep' = {
  scope: rg
  name: 'aca-env'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    infraSubnetId: network.outputs.acaSubnetId
    logAnalyticsCustomerId: observability.outputs.workspaceCustomerId
    logAnalyticsSharedKey: observability.outputs.workspaceSharedKey
  }
}

module broker 'modules/aca-broker.bicep' = {
  scope: rg
  name: 'aca-broker'
  params: {
    namePrefix: namePrefix
    location: location
    tags: tags
    acaEnvironmentId: acaEnv.outputs.environmentId
    brokerImage: brokerImage
    keyVaultName: keyvault.outputs.keyVaultName
    sandboxImage: sandboxImage
    sandboxSubnetId: network.outputs.sessionsSubnetId
    saasUrl: saasUrl
    insecureSaas: insecureSaas
    oidcIssuer: oidcIssuer
    oidcClientId: oidcClientId
    userAllowlist: userAllowlist
    acrName: acrName
  }
}

module frontDoor 'modules/front-door.bicep' = {
  scope: rg
  name: 'front-door'
  params: {
    namePrefix: namePrefix
    tags: tags
    brokerFqdn: broker.outputs.brokerFqdn
    brokerResourceId: acaEnv.outputs.environmentId
    portalHostname: portalHostname
  }
}

// ----- Outputs (for scripts) -----
output resourceGroupName string = rg.name
output frontDoorEndpoint string = frontDoor.outputs.endpointHostname
output brokerFqdn string = broker.outputs.brokerFqdn
output natGatewayPublicIp string = network.outputs.natPublicIp
output keyVaultName string = keyvault.outputs.keyVaultName
output logAnalyticsWorkspaceId string = observability.outputs.workspaceId
