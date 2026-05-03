// ACA Dynamic Sessions — custom container session pool (Kasm Chromium).
// Each user gets a Hyper-V isolated session, destroyed on logout/idle.
//
// Note: networkConfiguration uses VNet integration to ensure egress goes via NAT GW.
@description('Naming prefix.')
param namePrefix string
@description('Azure region.')
param location string
@description('Tags.')
param tags object
@description('Sandbox container image (Kasm chromium).')
param sandboxImage string
@description('Sessions-delegated subnet ID.')
param sessionsSubnetId string

@description('Max concurrent sessions across the pool.')
param maxConcurrentSessions int = 50

@description('Pre-warmed sessions (instant allocation).')
param readySessionInstances int = 3

@description('Cooldown (seconds) before idle session is destroyed.')
param cooldownPeriodInSeconds int = 300

resource pool 'Microsoft.App/sessionPools@2025-02-02-preview' = {
  name: 'sp-${namePrefix}-sandbox'
  location: location
  tags: tags
  properties: {
    poolManagementType: 'Dynamic'
    containerType: 'CustomContainer'
    scaleConfiguration: {
      maxConcurrentSessions: maxConcurrentSessions
      readySessionInstances: readySessionInstances
    }
    dynamicPoolConfiguration: {
      executionType: 'Timed'
      cooldownPeriodInSeconds: cooldownPeriodInSeconds
    }
    customContainerTemplate: {
      containers: [
        {
          name: 'sandbox'
          image: sandboxImage
          resources: {
            cpu: json('2.0')
            memory: '4Gi'
          }
          // Kasm exposes 6901 (HTTPS web UI) by default; broker proxies WS to this.
          // We expose plain HTTP from inside the sandbox (broker terminates TLS at Front Door).
          env: [
            { name: 'VNC_PW', value: 'unused-internal-only' }
            // Browser fingerprint normalization (egress region: Sweden -> en-US neutral).
            { name: 'LANG', value: 'en_US.UTF-8' }
            { name: 'TZ', value: 'Europe/Stockholm' }
            // Pin a generic, current Chrome on Windows UA. Update periodically.
            { name: 'CHROME_USER_AGENT', value: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36' }
            { name: 'CHROME_ACCEPT_LANG', value: 'en-US,en;q=0.9' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: 6901
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/'
                port: 6901
              }
              periodSeconds: 5
              failureThreshold: 60
            }
          ]
        }
      ]
      ingress: {
        targetPort: 6901
      }
    }
    sessionNetworkConfiguration: {
      status: 'EgressEnabled'
    }
  }
}

output poolName string = pool.name
output poolResourceId string = pool.id
output poolManagementEndpoint string = pool.properties.poolManagementEndpoint
