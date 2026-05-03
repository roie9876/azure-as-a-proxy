// Azure DNS Private Resolver — outbound endpoint into snet-dnsresolver.
// Ensures DNS resolution for the SaaS happens *from the egress region*, not from IL.
// The sandbox container's /etc/resolv.conf points at the resolver's inbound IP.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object
@description('VNet resource ID.')
param vnetId string
@description('DNS resolver subnet resource ID.')
param resolverSubnetId string

resource resolver 'Microsoft.Network/dnsResolvers@2023-07-01-preview' = {
  name: 'dnsr-${namePrefix}'
  location: location
  tags: tags
  properties: {
    virtualNetwork: {
      id: vnetId
    }
  }
}

resource outbound 'Microsoft.Network/dnsResolvers/outboundEndpoints@2023-07-01-preview' = {
  parent: resolver
  name: 'outbound'
  location: location
  properties: {
    subnet: {
      id: resolverSubnetId
    }
  }
}

output resolverId string = resolver.id
output outboundEndpointId string = outbound.id
