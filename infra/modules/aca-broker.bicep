// Session Broker — Container App in the ACA env.
// - System-assigned managed identity (used to call Dynamic Sessions API + read Key Vault).
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
@description('Key Vault name (for RBAC + reference).')
param keyVaultName string
@description('Sandbox container image (Kasm Chromium) for ACI provisioning.')
param sandboxImage string
@description('Subnet ID for sandbox ACIs (delegated to Microsoft.ContainerInstance/containerGroups).')
param sandboxSubnetId string
@description('OIDC issuer URL (empty = stub auth).')
param oidcIssuer string
@description('OIDC client ID (empty = stub auth).')
param oidcClientId string
@description('User allowlist (comma-separated).')
param userAllowlist string

@description('ACR name (empty = no creds, image must be public).')
param acrName string = ''

@description('Min replicas.')
param minReplicas int = 2
@description('Max replicas.')
param maxReplicas int = 5

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
      secrets: useAcr ? [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
      ] : []
      registries: useAcr ? [
        {
          server: '${acrName}.azurecr.io'
          username: acr.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ] : []
      ingress: {
        external: false
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
          env: [
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
            { name: 'AZURE_LOCATION', value: location }
            { name: 'SANDBOX_IMAGE', value: sandboxImage }
            { name: 'SANDBOX_SUBNET_ID', value: sandboxSubnetId }
            { name: 'ACR_NAME', value: acrName }
            { name: 'ACR_SERVER', value: useAcr ? '${acrName}.azurecr.io' : '' }
            { name: 'ACR_USERNAME', value: useAcr ? acr.listCredentials().username : '' }
            { name: 'ACR_PASSWORD', secretRef: useAcr ? 'acr-password' : null }
            { name: 'KEY_VAULT_NAME', value: keyVaultName }
            { name: 'OIDC_ISSUER', value: oidcIssuer }
            { name: 'OIDC_CLIENT_ID', value: oidcClientId }
            { name: 'USER_ALLOWLIST', value: userAllowlist }
            { name: 'WARM_POOL_SIZE', value: '2' }
            { name: 'SESSION_IDLE_TIMEOUT_SECONDS', value: '600' }
            { name: 'BROKER_LOG_LEVEL', value: 'INFO' }
          ]
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
// Broker MI -> Key Vault Secrets User (read OIDC client secret + signing key)
resource kvRef 'Microsoft.KeyVault/vaults@2024-04-01-preview' existing = {
  name: keyVaultName
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kvRef
  name: guid(kvRef.id, broker.id, 'kv-secrets-user')
  properties: {
    // Key Vault Secrets User
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: broker.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

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
