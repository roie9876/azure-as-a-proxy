// ACA managed environment: workload profiles, External VIP + VNET-injected,
// publicNetworkAccess=Disabled. This combination is required for FD Private Link:
//   - Workload profiles: prerequisite for FD-ACA Private Link integration.
//   - VNET-injection: needed so env outbound egresses via our NAT GW (static IP).
//   - internal=false (External VIP): apps get public-form FQDN
//     '<app>.<env>.<region>.azurecontainerapps.io' which is what the env edge proxy
//     accepts as Host header when traffic arrives via FD's shared Private Link.
//   - publicNetworkAccess=Disabled: blocks all public traffic; only PE traffic allowed.
//     (Per docs: PNA can be Disabled on External VIP envs and is changeable after creation.)
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
      internal: false
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
