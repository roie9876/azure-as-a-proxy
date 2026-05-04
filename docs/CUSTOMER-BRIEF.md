# SaaS Network-Identity Cloak — Customer Briefing

> A reusable Azure pattern that lets a customer run a third-party SaaS UI
> **without revealing to the end user that the app is Microsoft / Azure / Entra /
> Next.js**, and without revealing **the SaaS vendor's network identity** to the
> SaaS itself.

---

## 1. The problem (concrete, from a real test)

The customer captured a HAR file by browsing a SaaS test app directly:

```
https://arh2b5deb8dmcvcf.fz37.alb.azure.com/
```

That single direct browse leaks **eight** identifying signals to the user's
browser, network, DNS, and corporate proxy logs:

| # | Signal observed in HAR | Where the user / network sees it | What it tells the user |
|---|---|---|---|
| 1 | Hostname `*.alb.azure.com` | URL bar, DNS query, SNI on TLS handshake | "This is hosted on Azure Application Gateway for Containers" |
| 2 | `Server: Microsoft-Azure-Application-LB/AGC` | Every HTTP response | Explicit Azure ALB fingerprint |
| 3 | `x-powered-by: Next.js` | All HTML & API responses | The framework |
| 4 | `x-nextjs-cache`, `x-nextjs-prerender`, `x-nextjs-stale-time`, `vary: rsc, next-router-state-tree…` | HTML responses | Next.js internals & cache state |
| 5 | OAuth redirect to `login.microsoftonline.com/{tenantId}/oauth2/...` with `client_id` | Browser address bar, browser history, corporate proxy logs | Entra tenant GUID + app registration ID |
| 6 | Internal tenant GUIDs in API paths (`/api/v1/tenants/3d2df4d7-…`) | Network tab / DevTools | Internal tenant identifiers |
| 7 | TLS certificate SAN `*.fz37.alb.azure.com`, **self-signed**, **Subject == Issuer** | Certificate viewer + red `NET::ERR_CERT_AUTHORITY_INVALID` interstitial | Confirms Azure ALB and surfaces a scary cert warning to the end user |
| 8 | Origin-specific `etag` (`"rteu81xptt4n0"`) and `cache-control: s-maxage=31536000` | Network tab | Origin behavior fingerprint, immortal cache hint |

For the customer's use case (presenting the SaaS as if it were the customer's
own product, or hiding upstream provider details from end users / corporate
network monitoring), each of these is a leak that must be eliminated.

A side-by-side **F12 DevTools comparison** of these signals direct vs. via the
cloak is documented in [docs/F12-cloaking-test.md](F12-cloaking-test.md).

---

## 2. The design — "Path A: Network-Identity Cloak"

We do **not** try to bolt header-stripping onto the SaaS itself. Instead we put
**a sealed remote-browser sandbox between the user and the SaaS**, and the user
only sees a pixel stream from a generic-looking front door we control.

```
┌──────────────┐   HTTPS/WSS   ┌─────────────────┐   HTTPS/WSS   ┌──────────────┐
│ End user     │──────────────▶│ Customer-owned  │──────────────▶│ Session      │
│ (browser)    │  custom domain│ Front Door +WAF │ Private Link  │ Broker (ACA) │
│              │               │ (Premium)       │               │              │
└──────────────┘               └─────────────────┘               └──────┬───────┘
                                                                       │ ARM
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ Per-browser  │
                                                                │ Azure Cont.  │
                                                                │ Instance     │   user has NO
                                                                │ (custom      │   network path to
                                                                │  kiosk:      │   the SaaS
                                                                │  Xvfb+x11vnc+│
                                                                │  websockify+ │
                                                                │  noVNC+      │
                                                                │  Chromium)   │
                                                                └──────┬───────┘
                                                                       │ HTTPS
                                                                       ▼
                                                                ┌──────────────┐
                                                                │ NAT Gateway  │
                                                                │ (single      │   the SaaS sees
                                                                │  egress IP)  │   ONE IP, no user
                                                                └──────┬───────┘   identity
                                                                       │
                                                                       ▼
                                                                  Third-party SaaS
                                                                  (login.microsoftonline.com,
                                                                   *.alb.azure.com, etc.)
```

### What the user sees in their browser

- One hostname: the customer's **own custom domain** on Front Door
  (e.g. `workspace.customer.com`) — no `azure.com`, no `microsoft.com` in DNS.
- The customer's chosen TLS certificate (custom domain).
- A login page on the customer's domain (the broker's `/login`). Optionally
  federated to the customer's IdP (Entra / Auth0 / Okta / Keycloak), but the
  customer's IdP — never the SaaS's.
- After login: a single full-page **pixel stream** (WebSocket) of a remote
  Chromium browser. No `_next/`, no `/api/v1/`, no `Server` header, no
  framework fingerprints.

### What the SaaS sees

- Connections from a **single static egress IP** (the NAT Gateway public IP).
  All users → one IP. Cannot map back to any user, customer geo, or browser.
- A standard Chromium User-Agent, locked Accept-Language, no custom corporate
  TLS client cert, no extension fingerprints. Identical canvas/WebGL/font
  fingerprints across all sessions because it's the same container image.
- The user's authentication to the SaaS happens **inside the sandbox**, with
  the SaaS's own credentials — never the user's corporate identity.

### What the customer's network logs show

- Outbound traffic from end-user laptops to `workspace.customer.com` only.
- No `*.azure.com`, no `login.microsoftonline.com`, no SaaS vendor domains in
  DNS or proxy logs.

---

## 3. How each of the 8 leaks is eliminated

The core mechanism: **the SaaS HTTP response never traverses the user's browser**.
It is consumed by Chromium running inside the per-browser ACI; only the rendered
framebuffer pixels flow back over a single WebSocket. There is therefore nothing
for the user to inspect — no SaaS headers, no SaaS HTML, no SaaS cookies, no
SaaS redirects. F12 sees only broker + Front Door responses (verified against a
77-entry HAR — see [F12-cloaking-test.md](F12-cloaking-test.md)).

| Leak | How Path A removes it |
|------|----------------------|
| 1. `*.alb.azure.com` host | User connects only to the Front Door endpoint (custom domain in production). The SaaS hostname appears in **0** of 77 HAR entries. |
| 2. `Server: Microsoft-Azure-Application-LB/AGC` | Header is consumed by Chromium inside the sandbox; never re-emitted. The broker doesn't proxy the SaaS at the HTTP layer. |
| 3. `x-powered-by: Next.js` | Same as #2 — never reaches the user's browser. |
| 4. `x-nextjs-*` / `vary: rsc,next-router-…` | Same as #2/#3. |
| 5. `login.microsoftonline.com` | Entra / SaaS login is initiated **from inside the sandbox**, by Chromium running in ACI. The user's browser never navigates to `login.microsoftonline.com`. Browser history & corporate proxy DNS only ever see the customer's domain. |
| 6. Internal tenant GUIDs in API paths | All `/api/v1/...` calls are made by Chromium inside the sandbox to the SaaS. The user's browser only sees the WebSocket frames carrying pixels — no JSON, no GUIDs. |
| 7. TLS certificate SAN + self-signed cert error | Front Door presents a Microsoft-issued AFD cert (or the customer's cert on a custom domain). The self-signed ALB cert is never seen by the user. The kiosk Chromium tolerates the SaaS's self-signed cert via `INSECURE_SAAS=1` (`--ignore-certificate-errors` scoped to `$SAAS_URL`), so the cert-error interstitial — which would itself leak the SaaS hostname inside the streamed pixels — never appears. |
| 8. Origin `etag` / `cache-control` fingerprints | Same as #2 — those headers stay inside the sandbox. |

---

## 4. Architecture: components and why each one is there

| Component | Bicep file | Purpose | Cloak role |
|---|---|---|---|
| Resource group | [infra/main.bicep](../infra/main.bicep) | Container | — |
| Virtual Network (`/20`) | [infra/modules/network.bicep](../infra/modules/network.bicep) | All Azure traffic stays inside | Hides ALB + broker from public DNS |
| Subnet `snet-pe` | network.bicep | Private Endpoints (KV, Front Door origin) | — |
| Subnet `snet-aca` (delegated `Microsoft.App/environments`) | network.bicep | ACA env hosting broker | NAT GW egress |
| Subnet `snet-sessions` (delegated `Microsoft.ContainerInstance/containerGroups`) | network.bicep | Per-browser kiosk sandboxes (ACI) | NAT GW egress, NSG-isolated |
| Subnet `snet-dnsresolver` | network.bicep | Private DNS | — |
| **NAT Gateway + static Public IP** | network.bicep | All outbound from sandboxes leaves through ONE IP | **Egress identity unification** |
| NSG on `snet-sessions` | network.bicep | Sandbox can only egress to internet (via NAT GW); cannot talk to anything inside VNet except DNS resolver | Blast-radius containment |
| **Azure Front Door Premium + WAF** | [infra/modules/front-door.bicep](../infra/modules/front-door.bicep) | Customer's public entry point. Custom domain + cert. Strips Azure routing headers. WAF blocks bots/floods. | **Surface 1 cloak (UI hostname)** |
| Front Door → ACA via Private Link | front-door.bicep | Broker has no public IP | Broker is unreachable except via FD |
| **ACA Environment** (internal, VNet-injected) | [infra/modules/aca-environment.bicep](../infra/modules/aca-environment.bicep) | Hosts broker | Workload identity boundary |
| **Session Broker** (FastAPI) | [infra/modules/aca-broker.bicep](../infra/modules/aca-broker.bicep), [broker/](../broker/) | OIDC / stub login, allocates sandbox, mints attach token, proxies WebSocket → noVNC | Auth + orchestration |
| **Per-browser ACI sandbox** (custom kiosk image) | [sandbox/](../sandbox/), provisioned at runtime by broker | Debian 12-slim + Xvfb + fluxbox + x11vnc (`-nopw -localhost`) + websockify (port 6901 → VNC :5900) + bundled noVNC + Chromium `--kiosk --app=$SAAS_URL`. Pulled from ACR (`cloak-sandbox:kiosk-v2`). | Runs the actual browser; emits pixels only |
| **Warm pool of ACIs** (N=2 idle) | broker/app/sessions.py | Pre-provisioned sandboxes ready for instant claim | Removes the ~60 s cold-start wait at login |
| **Azure Container Registry** | (existing `acrcloak9f1d7e`) | Hosts broker + sandbox images | Private image distribution |
| **Key Vault** + Private Endpoint | [infra/modules/keyvault.bicep](../infra/modules/keyvault.bicep) | OIDC client secret, broker session secret | No secrets in env or git |
| **Private DNS Resolver** | [infra/modules/dns-resolver.bicep](../infra/modules/dns-resolver.bicep) | Lets sandbox resolve SaaS hostnames | Required when sandbox subnet is delegated |
| **Log Analytics + App Insights** | [infra/modules/observability.bicep](../infra/modules/observability.bicep) | Audit, debug | Compliance evidence |

### Why ACI per session (not ACA Dynamic Sessions)

Originally the design used ACA Dynamic Sessions (managed Hyper-V container
sandbox) running an off-the-shelf Kasm Chromium image. Two blockers surfaced:

1. **Dynamic Sessions disallows the privileged-mode operations** Kasm needs
   (its own X server / container init). Generic public images ran; Kasm
   crash-looped with "pods are crashing".
2. Even on plain ACI, Kasm's full desktop image (1.6 GB) is overkill for a
   single locked-down kiosk window and its login UI itself constitutes
   surface that has to be re-styled to avoid leaking "this is Kasm".

We pivoted to **per-session Azure Container Instances** orchestrated by the
broker via ARM REST, running a **custom 250 MB kiosk image** built in this
repo ([sandbox/Dockerfile](../sandbox/Dockerfile),
[sandbox/entrypoint.sh](../sandbox/entrypoint.sh)). Each ACI:

- Runs on the customer's VNet (subnet delegated to
  `Microsoft.ContainerInstance/containerGroups`).
- Has only a private IP (reachable from broker, not internet).
- Egresses through the NAT Gateway.
- Pulls the kiosk image from the customer's ACR with admin creds.
- Boots Xvfb → fluxbox → x11vnc (`-nopw -localhost` — no VNC password on the
  wire, only loopback) → websockify (`:6901 → 127.0.0.1:5900`) → Chromium
  `--kiosk --app=$SAAS_URL`.
- Honors `INSECURE_SAAS=1` (default in this PoC against the self-signed demo
  origin) — extracts the SaaS host, injects
  `--ignore-certificate-errors --unsafely-treat-insecure-origin-as-secure=$SAAS_URL`,
  so no cert-error interstitial ever renders inside the streamed pixels.
  Set `INSECURE_SAAS=0` for production CA-signed origins.
- Has no persistent storage — destroyed at session end.

### Why the warm pool

Cold ACI provision = ~60 s (image pull + kiosk boot). The broker keeps **N=2
idle ACIs always ready**. On user login:

1. Broker pops one idle ACI from the queue (sub-second).
2. Marks it claimed for that user.
3. Schedules a background task to provision a replacement.
4. Returns the attach URL to the user.

`WARM_POOL_SIZE` is configurable; raise to N=10 for higher concurrency.

---

## 5. Identity & secrets posture (what the customer's CISO will ask)

- **Broker has Managed Identity** (no client secrets in code).
- **Broker MI → Resource group: Contributor** — required to provision/delete
  ACIs at runtime. Tightenable to a custom role limited to
  `Microsoft.ContainerInstance/containerGroups/*` if the customer prefers
  least-privilege.
- **Broker MI → Key Vault: Secrets User** — read-only access to OIDC client
  secret + broker session-signing secret.
- **All inter-service traffic is private**: Front Door → ACA via Private Link;
  ACA → KV via Private Endpoint; ACI subnet has no public ingress.
- **Egress-only to internet** through NAT GW; no inbound from internet to any
  workload subnet.
- **PoC identity model:** stub auth + a per-browser UUID cookie
  (`cloak_browser_id`, HttpOnly, 8 h) becomes the user `sub`. This guarantees
  one isolated sandbox per browser, no cross-user state, and no shared
  Chromium profile — without requiring a real IdP for demo purposes.
- **Production identity model:** OIDC Authorization Code + PKCE against the
  customer's IdP (Entra / Auth0 / Okta / Keycloak). The cookie identity model
  collapses transparently into the OIDC `sub` claim.
- **VNC has no password on the wire** — `x11vnc -nopw -localhost` binds only to
  127.0.0.1 inside the ACI; the only way in is via the loopback websockify
  bridge, itself only reachable through the broker-issued attach token.
- **WAF in Prevention mode** with Microsoft Default Rule Set + Bot Manager.

---

## 6. What "shipped today" means (deployment status)

The repo at https://github.com/roie9876/azure-as-a-proxy contains:

- **Subscription-scoped Bicep** that creates everything in one
  `az deployment sub create` call. Idempotent.
- [`scripts/build-and-push.sh`](../scripts/build-and-push.sh) — builds broker
  + custom kiosk sandbox images (`linux/amd64`) into ACR.
- [`scripts/deploy.sh`](../scripts/deploy.sh) — runs the deployment with
  parameter file.
- [`scripts/smoke-test.sh`](../scripts/smoke-test.sh) — automated check that
  confirms the SaaS-fingerprint headers are absent on the Front Door endpoint
  and that privacy headers (`Referrer-Policy`, `X-Content-Type-Options`,
  `X-Frame-Options`, `Permissions-Policy`, `Content-Security-Policy`) are
  present on broker responses.
- [`docs/F12-cloaking-test.md`](F12-cloaking-test.md) — reproducible
  side-by-side proof of what the browser DevTools sees direct vs. via the
  cloak.

Current deployed PoC values (Sweden Central):

- Front Door endpoint: `ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net`
- Sandbox image: `acrcloak9f1d7e.azurecr.io/cloak-sandbox:kiosk-v2`
- Pinned demo SaaS URL: `https://arh2b5deb8dmcvcf.fz37.alb.azure.com/`

Customer can fork, adjust [infra/main.bicepparam](../infra/main.bicepparam) (region,
custom domain, OIDC config, SaaS URL), point their ACR at it, and
`azd up`-style deploy.

---

## 7. What still requires customer-side action

| Item | Why it can't be automated |
|------|--------------------------|
| Custom domain (e.g. `workspace.customer.com`) on Front Door | Customer owns DNS zone; needs CNAME validation + cert. Param `portalHostname` supports it. |
| OIDC IdP registration | Customer chooses Entra / Auth0 / Okta / Keycloak. Provide issuer URL + client ID; secret goes in Key Vault. |
| Allowlist of users (optional) | `userAllowlist` param: comma-separated list of `sub` / email / UPN. |
| Front Door → Private Endpoint approval | First deploy creates a pending PE connection on the ACA env; customer-tenant approval is required (one-time, in Portal or `az network private-endpoint-connection approve`). |
| SaaS-side credentials | Stored in Key Vault; injected into sandbox by the broker at session start. |

---

## 8. Cost snapshot (Sweden Central, list price, illustrative)

| Resource | Approx monthly |
|---|---|
| Front Door Premium (1 endpoint, low traffic) | ~$330 |
| ACA env (Consumption, 1 broker replica) | ~$30 |
| ACI warm pool (2× 2 vCPU 4 GiB, idle 24/7) | ~$180 |
| ACI per-session (claimed, ~hours/day per active user) | usage-based |
| NAT Gateway + Static IP | ~$45 |
| Key Vault, Log Analytics, App Insights, DNS Resolver | ~$30 |
| **Baseline total (no users)** | **~$615/mo** |

Per active user incremental: 2 vCPU × hours-active × ~$0.05/hr = a few dollars
per user-day. Warm-pool size tunes cold-start vs idle cost.

---

## 9. What proves it works (the demo flow)

1. **Before deployment** — show the HAR captured against
   `arh2b5deb8dmcvcf.fz37.alb.azure.com`: 8 leaks visible, plus the red
   self-signed-cert interstitial.
2. **After deployment** — `scripts/smoke-test.sh <frontDoorHostname>` passes:
   SaaS-fingerprint headers absent, privacy headers present.
3. **Live demo** — open `https://<frontDoorHostname>` in a clean browser:
   - URL bar: customer's / Front Door domain only. Microsoft-issued cert,
     green padlock.
   - DevTools → Network: only requests to the FD endpoint. The full HAR shows
     `0/77` requests touching the SaaS hostname.
   - DevTools → Network: one long-lived WebSocket carrying VNC framebuffer
     binary frames. No SaaS HTML, JS, JSON, headers, cookies or redirects.
   - DevTools → Application → cookies: only the broker's HttpOnly session
     cookie + per-browser `cloak_browser_id` UUID. Nothing from the SaaS.
   - SaaS-side audit log: connections from one IP (NAT GW), one Chromium UA.
4. **Forensic proof** — see [docs/F12-cloaking-test.md](F12-cloaking-test.md)
   for the complete header-by-header comparison and the Python one-liner the
   customer can run on their own captured HARs.

---

## 10. Open items / roadmap

- **Parameterize the kiosk image + SaaS URL in Bicep.** Currently
  `SANDBOX_IMAGE`, `SAAS_URL` and `INSECURE_SAAS` are set imperatively via
  `az containerapp update`; they need to move into
  [infra/modules/aca-broker.bicep](../infra/modules/aca-broker.bicep) so a
  fresh `az deployment sub create` doesn't clobber them.
- Replace in-memory warm-pool maps with **Redis (Azure Cache for Redis)** for
  HA across multiple broker replicas.
- Switch Key Vault `publicNetworkAccess` to **Disabled** + dedicated PE.
- Tighten broker MI from Contributor to a **custom role** scoped to
  `Microsoft.ContainerInstance/containerGroups/*`.
- Add **JWKS verification** of OIDC `id_token` (currently trusts UserInfo
  endpoint) and replace the cookie-only stub auth with the real OIDC flow.
- Replace the self-signed demo SaaS cert with a CA-signed cert, then flip
  `INSECURE_SAAS=0` so the kiosk Chromium enforces full TLS verification.
- Optional: per-tenant ACR pull token instead of admin creds.
- Optional: session recording (Xvfb → ffmpeg → blob) for compliance.

---

## 11. Lessons learned during the build (for the customer's eng team)

- **Azure Container Apps Dynamic Sessions does not allow privileged-mode
  containers.** Kasm Chromium needs an internal X server / init system that
  Dynamic Sessions' Hyper-V sandbox blocks. Generic public images run fine,
  but Kasm crash-loops with "pods are crashing". Mitigation: switched to
  per-session Azure Container Instances (still in the customer's VNet, still
  egressing through NAT GW).
- **Kasm desktop image is the wrong primitive.** It bundles a desktop
  environment, login UI and chrome-around-Chromium that themselves leak
  identity ("this is a Kasm session"). We replaced it with a 250 MB Debian
  12-slim image that is just `Xvfb + fluxbox + x11vnc + websockify + noVNC +
  Chromium --kiosk --app=$SAAS_URL`. No login UI inside the sandbox; the
  user only ever sees the SaaS pixels.
- **A self-signed origin cert breaks the cloak by leaking the hostname inside
  the pixels.** `NET::ERR_CERT_AUTHORITY_INVALID` shows the SaaS host in
  bold inside the streamed Chromium window. Mitigation: `INSECURE_SAAS=1`
  injects `--ignore-certificate-errors --unsafely-treat-insecure-origin-as-secure=$SAAS_URL`
  scoped strictly to the pinned SaaS URL. Production fix: use a CA-signed cert
  on the SaaS origin and turn the flag off.
- **Front Door rule limit: 10 actions per rule.** We needed to strip 8
  identifying headers and add 5 privacy headers (13 actions). Mitigation:
  split into two sibling rules under the same RuleSet (`stripHeaders` +
  `privacyHeaders`), both attached to the same route.
- **ACR `anonymousPullEnabled=true` still returns 401 from ACA pulls** in
  some configurations. Mitigation: configure explicit registry credentials on
  ACI with the ACR admin user (rotatable / can be replaced with a token).
- **Subnet delegations are mutually exclusive.** A subnet delegated to ACA
  cannot also host ACI. We use separate subnets:
  `snet-aca` (`Microsoft.App/environments`) and
  `snet-sessions` (`Microsoft.ContainerInstance/containerGroups`).
- **Front Door Premium provisioning takes 8–15 minutes** on first deploy.
  This is normal — subsequent updates are faster.
- **`x11vnc -nopw` is correct here** because the only path to the VNC port is
  loopback inside the ACI. Adding a VNC password would create a credential
  that has to be transported to the user's browser through the WebSocket
  upgrade — which is exactly the kind of leak the cloak is designed to avoid.
  HAR analysis confirms no password-bearing traffic on the wire.
