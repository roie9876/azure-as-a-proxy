using './main.bicep'

param namePrefix = 'cloak'
param location = 'swedencentral'

param tags = {
  project: 'saas-network-identity-cloak'
  path: 'A'
  owner: 'platform'
  env: 'dev'
}

param vnetAddressSpace = '10.80.0.0/20'

// Set these after `scripts/build-and-push.sh` populates ACR.
param brokerImage = 'acrcloak9f1d7e.azurecr.io/cloak-broker:latest'
param sandboxImage = 'acrcloak9f1d7e.azurecr.io/cloak-sandbox:latest'
param acrName = 'acrcloak9f1d7e'

// Stub auth in PoC — fill in real values when wiring an IdP.
param oidcIssuer = ''
param oidcClientId = ''
param userAllowlist = ''

// Bind your own custom domain when ready; until then Front Door serves on *.azurefd.net.
param portalHostname = ''
