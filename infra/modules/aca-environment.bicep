// ACA managed environment: workload profiles, internal-only ingress, VNet-injected.
// Internal ingress is required so Front Door reaches it ONLY via Private Endpoint.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object
@description('Resource ID of the ACA-delegated subnet.')
param infraSubnetId string
@description('Log Analytics customer (workspace) ID.')
param logAnalyticsCustomerId string
@secure()
@description('Log Analytics primary shared key.')
param logAnalyticsSharedKey string

resource env 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: 'cae-${namePrefix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: infraSubnetId
      internal: true
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    publicNetworkAccess: 'Disabled'
  }
}

output environmentId string = env.id
output environmentName string = env.name
output environmentDefaultDomain string = env.properties.defaultDomain
output environmentStaticIp string = env.properties.staticIp
