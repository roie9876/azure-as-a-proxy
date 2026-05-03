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
@description('Session pool poolManagementEndpoint URL.')
param sessionPoolManagementEndpoint string
@description('Session pool resource ID (for RBAC).')
param sessionPoolResourceId string
@description('OIDC issuer URL (empty = stub auth).')
param oidcIssuer string
@description('OIDC client ID (empty = stub auth).')
param oidcClientId string
@description('User allowlist (comma-separated).')
param userAllowlist string

@description('Min replicas.')
param minReplicas int = 2
@description('Max replicas.')
param maxReplicas int = 5

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
            { name: 'SESSION_POOL_ENDPOINT', value: sessionPoolManagementEndpoint }
            { name: 'SESSION_POOL_RESOURCE_ID', value: sessionPoolResourceId }
            { name: 'KEY_VAULT_NAME', value: keyVaultName }
            { name: 'OIDC_ISSUER', value: oidcIssuer }
            { name: 'OIDC_CLIENT_ID', value: oidcClientId }
            { name: 'USER_ALLOWLIST', value: userAllowlist }
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

// Broker MI -> Session pool: "Azure Container Apps Session Executor" role (built-in).
// Role definition GUID: 0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0
resource sessionsExecutor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: resourceGroup()
  name: guid(sessionPoolResourceId, broker.id, 'sessions-executor')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0')
    principalId: broker.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output brokerFqdn string = broker.properties.configuration.ingress.fqdn
output brokerName string = broker.name
output brokerPrincipalId string = broker.identity.principalId
