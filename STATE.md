# Cloak POC — Current State

**Last updated:** 2026-06-24, autonomous session.
**Branch:** `main`, last commit `8195670` (pushed).

## ✅ Working end-to-end

```
$ curl -i https://ep-cloak-gxiyt3jjny2oe-bne2a8hmffcuhnfw.b02.azurefd.net/healthz
HTTP/2 200
{"ok":true}
```

- **SaaS target:** `https://www.ynet.co.il/` (set in `infra/main.bicepparam`, `insecureSaas=0`).
- **Desktop + mobile rendering verified** end-to-end through Front Door: a laptop
  gets a desktop sandbox; a phone (detected via `Sec-CH-UA-Mobile`/UA at the broker)
  gets a 390x844 mobile-emulated sandbox so the SaaS serves its mobile layout.
- File upload bar renders on both desktop (top-right card) and mobile (bottom bar).

`bash scripts/smoke-test.sh ep-cloak-gxiyt3jjny2oe-bne2a8hmffcuhnfw.b02.azurefd.net`:
- SaaS-identity headers stripped + privacy headers applied (see `infra/modules/front-door.bicep`).
- Static egress IP: NAT GW `9.223.32.155`.

## Live Azure resources

| Resource | Name |
|---|---|
| Subscription | `ed2fda1d-8138-4434-866b-d183eaaae104` (ME-MngEnvMCAP338326-robenhai-2) |
| RG | `rg-cloak-swedencentral` |
| Region | `swedencentral` |
| FD endpoint | `ep-cloak-gxiyt3jjny2oe-bne2a8hmffcuhnfw.b02.azurefd.net` |
| FD profile | `afd-cloak` (Premium, origin-response-timeout **120s**) |
| ACA env | `cae-cloak` (workload-profile, **internal=false**, PNA=Disabled) |
| ACA env defaultDomain | `ashysmoke-d4db3aad.swedencentral.azurecontainerapps.io` |
| Broker app | `ca-cloak-broker` (external=true; active rev `ca-cloak-broker--m390181559`) |
| Broker FQDN | `ca-cloak-broker.ashysmoke-d4db3aad.swedencentral.azurecontainerapps.io` |
| ACR | `acrcloakc626e2.azurecr.io` (admin-enabled; images built via `az acr build`) |
| Images | `acrcloakc626e2.azurecr.io/cloak-broker:v1`, `…/cloak-sandbox:v1` |
| NAT GW public IP | `9.223.32.155` |
| FD shared PE | Approved |

> No Key Vault in this deployment — the broker falls back to a per-deploy session
> secret (fine for the single active replica). Add KV + PE before multi-replica.

## Mobile / device-aware rendering (README §5.6)

- Broker reads the real client headers (`Sec-CH-UA-Mobile` / User-Agent) and
  provisions the per-browser sandbox Chromium in a **desktop** or **mobile** profile.
- **Mobile geometry: `390x844` @ DSF 1.0.** On kiosk Chromium/Xvfb,
  `--force-device-scale-factor` does NOT shrink the CSS layout viewport (the
  viewport equals the physical Xvfb width), so the screen width itself must be a
  real phone width (<768px) for the SaaS to serve mobile. DSF>1 only over-sizes
  the window and clips it — keep DSF=1.
- **Per-profile warm pools:** `warm_pool_size` (desktop=2) + `mobile_warm_pool_size`
  (mobile=1). Mobile needs a warm pool because a cold ACI start exceeds the FD
  origin-response timeout → 504. Tunables in `broker/app/config.py`.

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

- ~~Replace placeholder `cloak-sandbox:latest` image~~ **DONE** — real kiosk image
  (Xvfb + x11vnc + websockify + noVNC + Chromium `--kiosk --app=$SAAS_URL` +
  file-inbox + Cloak picker extension) in `sandbox/`, plus device-aware
  desktop/mobile emulation.
- Key Vault `publicNetworkAccess=Disabled` + Private Endpoint (no KV deployed yet)
- JWKS verification in broker auth
- Narrower than RG-Contributor RBAC for broker UAMI (e.g.,
  `Microsoft.ContainerInstance/containerGroups/*` only)
- Custom domain on FD endpoint
- Redis-backed warm pool for HA (warm pools are in-memory per replica today)
- WAF policy on FD (currently default profile-managed rules only)
- iPad detection: modern iPad Safari reports a desktop UA + no `Sec-CH-UA-Mobile`,
  so it's treated as desktop; needs a touch-capability hint to map to mobile.

## Useful commands

```bash
# Smoke test
bash scripts/smoke-test.sh ep-cloak-gxiyt3jjny2oe-bne2a8hmffcuhnfw.b02.azurefd.net

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
