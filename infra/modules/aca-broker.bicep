// Session Broker — Container App in the ACA env.
// - System-assigned managed identity (used to call ARM to provision ACI sandboxes).
// - Internal ingress only; Front Door reaches it via Private Endpoint to the ACA env.
// - WebSocket-aware ingress.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object
@description('ACA managed environment resource ID.')
param acaEnvironmentId string
@description('Broker container image (FastAPI).')
param brokerImage string
@description('Sandbox container image for ACI provisioning (custom kiosk: Xvfb+x11vnc+websockify+noVNC+Chromium).')
param sandboxImage string
@description('Subnet ID for sandbox ACIs (delegated to Microsoft.ContainerInstance/containerGroups).')
param sandboxSubnetId string
@description('Target SaaS URL pinned per sandbox. Passed to kiosk Chromium as --app=$SAAS_URL.')
param saasUrl string
@description('\'1\' = ignore TLS errors for SAAS_URL (PoC against self-signed); \'0\' = strict TLS verification.')
@allowed([ '0', '1' ])
param insecureSaas string = '0'

@description('ACR name (empty = no creds, image must be public).')
param acrName string = ''

// IMPORTANT: broker keeps per-user sandbox allocations and the warm pool in
// process memory (sessions.py module-level dicts). Running >1 replica causes a
// split-brain where Front Door round-robins requests and replica B doesn't see
// the sandbox allocated by replica A -> 409 "no sandbox; visit /session first"
// on /vnc.html, /websockify, /upload. Pin to 1 until session state is moved
// to a shared store (Redis/Cosmos) or sticky sessions are added.
@description('Min replicas. Keep at 1 until broker session state is externalized.')
param minReplicas int = 1
@description('Max replicas. Keep at 1 until broker session state is externalized.')
param maxReplicas int = 1

@description('Stable secret used by the broker to sign the cloak_session routing cookie. Defaults to a deployment-time GUID; set explicitly to keep cookies valid across redeploys.')
@secure()
param brokerSessionSecret string = newGuid()

@description('Enable broker-mediated /upload endpoint (user picks file in their own browser, broker forwards to sandbox file-inbox). See docs/UPLOAD.md.')
param uploadEnabled bool = true
@description('Per-file upload cap in bytes. Front Door / ACA ingress hard limit is 100 MB.')
param uploadMaxBytes int = 100 * 1024 * 1024
@description('Per-browser-session aggregate upload cap in bytes.')
param uploadSessionMaxBytes int = 500 * 1024 * 1024
@description('Optional shared secret broker<->sandbox file-inbox. Empty = rely on VNet isolation only.')
@secure()
param sandboxInboxToken string = ''

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = if (!empty(acrName)) {
  name: acrName
}

var useAcr = !empty(acrName)

resource broker 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-${namePrefix}-broker'
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: acaEnvironmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: concat(
        useAcr ? [
          {
            name: 'acr-password'
            value: acr.listCredentials().passwords[0].value
          }
        ] : [],
        [
          {
            name: 'broker-session-secret'
            value: brokerSessionSecret
          }
        ],
        empty(sandboxInboxToken) ? [] : [
          {
            name: 'sandbox-inbox-token'
            value: sandboxInboxToken
          }
        ]
      )
      registries: useAcr ? [
        {
          server: '${acrName}.azurecr.io'
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ] : []
      ingress: {
        // external=true means the app is exposed on the env's edge proxy under
        // its public-form FQDN. Combined with env publicNetworkAccess=Disabled,
        // the only way to reach this app is via FD's shared Private Link
        // (groupId=managedEnvironments). FD originHostHeader = the runtime fqdn,
        // which the env edge proxy uses to route to this specific container app.
        external: true
        targetPort: 8000
        transport: 'auto' // WebSocket support
        allowInsecure: false
        traffic: [
          { weight: 100, latestRevision: true }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'broker'
          image: brokerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat([
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
            { name: 'AZURE_LOCATION', value: location }
            { name: 'SANDBOX_IMAGE', value: sandboxImage }
            { name: 'SANDBOX_SUBNET_ID', value: sandboxSubnetId }
            { name: 'SAAS_URL', value: saasUrl }
            { name: 'INSECURE_SAAS', value: insecureSaas }
            { name: 'ACR_NAME', value: acrName }
            { name: 'ACR_SERVER', value: useAcr ? '${acrName}.azurecr.io' : '' }
            { name: 'ACR_USERNAME', value: useAcr ? acr.listCredentials().username : '' }
            { name: 'ACR_PASSWORD', secretRef: useAcr ? 'acr-password' : null }
            { name: 'WARM_POOL_SIZE', value: '2' }
            { name: 'SESSION_IDLE_TIMEOUT_SECONDS', value: '600' }
            { name: 'BROKER_LOG_LEVEL', value: 'INFO' }
            { name: 'BROKER_SESSION_SECRET', secretRef: 'broker-session-secret' }
            { name: 'UPLOAD_ENABLED', value: uploadEnabled ? 'true' : 'false' }
            { name: 'UPLOAD_MAX_BYTES', value: string(uploadMaxBytes) }
            { name: 'UPLOAD_SESSION_MAX_BYTES', value: string(uploadSessionMaxBytes) }
            { name: 'SANDBOX_INBOX_PORT', value: '6902' }
          ],
          empty(sandboxInboxToken) ? [] : [
            { name: 'SANDBOX_INBOX_TOKEN', secretRef: 'sandbox-inbox-token' }
          ])
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8000
              }
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: 8000
              }
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
}

// ---- RBAC ----
// Broker MI -> Resource group: Contributor (provision/delete ACI sandboxes).
// Role definition GUID: b24988ac-6180-42a0-ab88-20f7382dd24c (Contributor)
resource rgContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(resourceGroup().id, broker.id, 'rg-contributor')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b24988ac-6180-42a0-ab88-20f7382dd24c')
    principalId: broker.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output brokerFqdn string = broker.properties.configuration.ingress.fqdn
output brokerName string = broker.name
output brokerPrincipalId string = broker.identity.principalId
