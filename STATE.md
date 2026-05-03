# Cloak POC — Current State

**Last updated:** 2026-05-03, autonomous session.
**Branch:** `main`, last commit `13147d6` (pushed).

## ✅ Working end-to-end

```
$ curl -i https://ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net/healthz
HTTP/2 200
{"ok":true}
```

`bash scripts/smoke-test.sh ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net` → **PASSED**:
- 7 SaaS-identity headers stripped (Server, X-Powered-By, X-Request-Id,
  X-Correlation-Id, X-Nextjs-Cache, X-Nextjs-Prerender, X-Nextjs-Stale-Time)
- 4 privacy headers applied (Referrer-Policy, X-Content-Type-Options,
  X-Frame-Options, Permissions-Policy)
- Static egress IP unchanged: NAT GW `20.240.234.198`

## Live Azure resources

| Resource | Name |
|---|---|
| Subscription | `f81ed7c0-efed-4b77-b948-b85407bdb710` |
| RG | `rg-cloak-swedencentral` |
| Region | `swedencentral` |
| FD endpoint | `ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net` |
| FD profile | `afd-cloak` (Premium) |
| ACA env | `cae-cloak` (workload-profile, **internal=false**, PNA=Disabled) |
| ACA env defaultDomain | `gentleisland-d16f31b9.swedencentral.azurecontainerapps.io` |
| Broker app | `ca-cloak-broker` (external=true, port 8000, 2 replicas Healthy) |
| Broker FQDN | `ca-cloak-broker.gentleisland-d16f31b9.swedencentral.azurecontainerapps.io` |
| ACR | `acrcloak9f1d7e.azurecr.io` |
| NAT GW public IP | `20.240.234.198` |
| FD shared PE | Approved |
| Broker UAMI | `id-cloak-broker` (Contributor on RG, Key Vault Secrets User on KV) |

## Key architectural decisions (documented in code comments)

1. **ACA env MUST be `internal: false` + `publicNetworkAccess: Disabled`** for FD
   shared Private Link to work. Internal VIP envs only support PNA=Disabled but
   their `*.internal.<env>.…` FQDN doesn't pass the env edge proxy's TLS SNI when
   traffic arrives via FD's PE → 421 Misdirected Request → "Container App -
   Unavailable" 404. The `internal` flag is **immutable**, so changing it requires
   env recreation.
2. **Container app must be `external: true`** so it gets the public-form FQDN. The
   env still has no public reachability because PNA=Disabled.
3. **FD origin hostName/originHostHeader = container app's `properties.configuration.ingress.fqdn`**
   (the public-form name).
4. **FD reserved response headers cannot be modified by rules engine** (Via,
   X-Azure-Ref, X-Cache, X-MSEdge-Ref, X-Forwarded-*, etc. — full list in
   `infra/modules/front-door.bicep` top comment). These will appear in responses;
   they reveal "Azure CDN presence" but no SaaS identity. Documented as residual.
5. **Rule actions limit: 10 actions per rule** → split strip + privacy into 2 rules.
6. **`Server: ""` overwrite is rejected** by FD (empty value not allowed); use
   Delete action instead.

## Path A still TODO (deferred)

- Replace placeholder `cloak-sandbox:latest` image (currently exits 0 in 4s,
  no listening port) with a real Kasm/noVNC Chromium container. Warmer creates
  ACIs successfully but they terminate immediately. Broker code is ready.
- Key Vault `publicNetworkAccess=Disabled` + Private Endpoint
- JWKS verification in broker auth
- Narrower than RG-Contributor RBAC for broker UAMI (e.g.,
  `Microsoft.ContainerInstance/containerGroups/*` only)
- Custom domain on FD endpoint
- Redis-backed warm pool for HA
- WAF policy on FD (currently default profile-managed rules only)

## Useful commands

```bash
# Smoke test
bash scripts/smoke-test.sh ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net

# Broker logs
az containerapp logs show -g rg-cloak-swedencentral -n ca-cloak-broker --tail 50 --type console

# List active sandbox ACIs
az resource list -g rg-cloak-swedencentral \
  --resource-type Microsoft.ContainerInstance/containerGroups \
  --query "[].{name:name, state:properties.instanceView.state}" -o table

# Redeploy infra
az deployment sub create --name cloak-$(date +%Y%m%d-%H%M%S) \
  --location swedencentral \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam

# Check FD origin/PE
az afd origin show -g rg-cloak-swedencentral --profile-name afd-cloak \
  --origin-group-name og-broker --origin-name broker -o json

az network private-endpoint-connection list \
  --resource-group rg-cloak-swedencentral --name cae-cloak \
  --type Microsoft.App/managedEnvironments \
  --query "[].{name:name, status:properties.privateLinkServiceConnectionState.status}" -o table
```
