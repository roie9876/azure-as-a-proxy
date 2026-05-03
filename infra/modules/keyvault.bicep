// Key Vault for: broker session-signing key, OIDC client secret, future cert.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object

resource kv 'Microsoft.KeyVault/vaults@2024-04-01-preview' = {
  name: take('kv-${namePrefix}-${uniqueString(resourceGroup().id)}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled' // Tighten to 'Disabled' + Private Endpoint after broker is wired.
  }
}

output keyVaultName string = kv.name
output keyVaultId string = kv.id
output keyVaultUri string = kv.properties.vaultUri
