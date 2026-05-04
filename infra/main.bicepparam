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
// IMPORTANT: ACR is NOT provisioned by this Bicep — pre-create it (or change `acrName`)
// and push the broker + sandbox images before deploying.
param brokerImage = 'acrcloak9f1d7e.azurecr.io/cloak-broker:latest'
param sandboxImage = 'acrcloak9f1d7e.azurecr.io/cloak-sandbox:kiosk-v2'
param acrName = 'acrcloak9f1d7e'

// Pin the SaaS the kiosk Chromium will load. Required.
param saasUrl = 'https://arh2b5deb8dmcvcf.fz37.alb.azure.com/'
// '1' for the self-signed PoC origin; flip to '0' once SaaS is on a CA-signed cert.
param insecureSaas = '1'

// Public source IPs allowed to reach the FD endpoint (WAF custom rule).
// Empty = no IP restriction (current PoC). Fill with corp egress CIDRs to lock down.
// Example: ['1.2.3.4/32', '5.6.7.0/24']
param allowedSourceIps = []

// Bind your own custom domain when ready; until then Front Door serves on *.azurefd.net.
param portalHostname = ''
