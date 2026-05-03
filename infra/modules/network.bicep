// Hub VNet: ACA infra subnet, Sessions subnet (delegated), DNS Resolver subnet, PE subnet.
// Egress: NAT Gateway + Standard Public IP attached at the *subnet* level for sessions + aca.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object
@description('VNet address space.')
param vnetAddressSpace string

// --- Public IP for NAT Gateway (egress) ---
resource natPip 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: 'pip-${namePrefix}-natgw'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    idleTimeoutInMinutes: 10
  }
}

resource natGw 'Microsoft.Network/natGateways@2024-05-01' = {
  name: 'natgw-${namePrefix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: natPip.id
      }
    ]
  }
}

// --- NSGs ---
resource nsgAca 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${namePrefix}-aca'
  location: location
  tags: tags
  properties: {
    securityRules: [
      // ACA platform requirements: outbound 443 + 9000 + 5671/5672 (handled by service tags).
      {
        name: 'AllowMcrOut'
        properties: {
          priority: 100
          direction: 'Outbound'
          access: 'Allow'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: 'MicrosoftContainerRegistry'
          destinationPortRange: '443'
        }
      }
      {
        name: 'AllowAzureFrontDoorBackendInbound'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefix: 'AzureFrontDoor.Backend'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '443'
        }
      }
    ]
  }
}

resource nsgSessions 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: 'nsg-${namePrefix}-sessions'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

// --- VNet ---
// /20 default = 4096 addresses split across 4 subnets:
//   .0/24 PE
//   .16/22 ACA workload-profile env (1024 addrs — required by ACA)
//   .32/22 Sessions (1024 addrs — Dynamic Sessions pool requires its own /22)
//   .48/28 DNS Resolver inbound/outbound
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${namePrefix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressSpace
      ]
    }
    subnets: [
      {
        name: 'snet-pe'
        properties: {
          addressPrefix: cidrSubnet(vnetAddressSpace, 24, 0)
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'snet-aca'
        properties: {
          addressPrefix: cidrSubnet(vnetAddressSpace, 22, 1)
          natGateway: {
            id: natGw.id
          }
          networkSecurityGroup: {
            id: nsgAca.id
          }
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-sessions'
        properties: {
          addressPrefix: cidrSubnet(vnetAddressSpace, 22, 2)
          natGateway: {
            id: natGw.id
          }
          networkSecurityGroup: {
            id: nsgSessions.id
          }
          delegations: [
            {
              name: 'sessions-delegation'
              properties: {
                serviceName: 'Microsoft.App/sessionPools'
              }
            }
          ]
        }
      }
      {
        name: 'snet-dnsresolver'
        properties: {
          addressPrefix: cidrSubnet(vnetAddressSpace, 28, 48)
          delegations: [
            {
              name: 'dnsresolver-delegation'
              properties: {
                serviceName: 'Microsoft.Network/dnsResolvers'
              }
            }
          ]
        }
      }
    ]
  }
}

output vnetId string = vnet.id
output vnetName string = vnet.name
output peSubnetId string = '${vnet.id}/subnets/snet-pe'
output acaSubnetId string = '${vnet.id}/subnets/snet-aca'
output sessionsSubnetId string = '${vnet.id}/subnets/snet-sessions'
output dnsResolverSubnetId string = '${vnet.id}/subnets/snet-dnsresolver'
output natPublicIp string = natPip.properties.ipAddress
