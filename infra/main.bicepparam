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

// Default images live in the public GHCR for this repo — no ACR, no creds, no
// build step. A customer cloning the repo can `./scripts/deploy.sh` straight
// away and ACA will pull the images anonymously from GitHub Container Registry.
//
// To use your own private ACR instead:
//   1. set `acrName = '<your-acr>'`
//   2. set brokerImage/sandboxImage to '<your-acr>.azurecr.io/...'
//   3. push images via `scripts/build-and-push.sh <your-acr>`
param brokerImage  = 'acrcloakc626e2.azurecr.io/cloak-broker:v1'
param sandboxImage = 'acrcloakc626e2.azurecr.io/cloak-sandbox:v1'
param acrName      = 'acrcloakc626e2'   // admin-enabled ACR built via az acr build

// Pin the SaaS the kiosk Chromium will load. Required.
param saasUrl = 'https://www.ynet.co.il/'
// '1' for the self-signed PoC origin; flip to '0' once SaaS is on a CA-signed cert.
param insecureSaas = '0'

// Public source IPs allowed to reach the FD endpoint (WAF custom rule).
// Empty = no IP restriction (current PoC). Fill with corp egress CIDRs to lock down.
// Example: ['1.2.3.4/32', '5.6.7.0/24']
param allowedSourceIps = []

// Bind your own custom domain when ready; until then Front Door serves on *.azurefd.net.
param portalHostname = ''
