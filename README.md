# Azure SaaS Network-Identity Cloak — Path A

> **Goal:** Let a fixed population of corporate users (count: X) access an unknown third-party SaaS *without* the user being able to determine the destination's network identity (URL, DNS, tenant ID, headers, cookies, body) from any tool on their endpoint.

---

## 1. Topology

![Architecture topology](topology.png?v=3)

Source: [topology.drawio](topology.drawio) — open in [draw.io](https://app.diagrams.net) or the VS Code drawio extension.

Autoscale model (broker replicas + per-browser sandboxes): [autoscale.png](autoscale.png) · source [autoscale.drawio](autoscale.drawio).

To regenerate the PNGs after editing the `.drawio` files:

```bash
/Applications/draw.io.app/Contents/MacOS/draw.io --export -f png --scale 2 topology.drawio
/Applications/draw.io.app/Contents/MacOS/draw.io --export -f png --scale 2 autoscale.drawio
```

---

## 2. Solution overview — Azure-native PaaS RBI

The topology above implements **Remote Browser Isolation (RBI)** on Azure-native PaaS. The SaaS HTML/JS/JSON **never reaches the user's browser** — it is consumed by Chromium running inside a per-browser Azure Container Instance, and only the rendered framebuffer pixels stream back over a single WebSocket. Tenant IDs, response bodies, cookies, and headers therefore have no path out of the sandbox.

| Layer | Service | Role |
|---|---|---|
| Public surface | **Azure Front Door Premium** + WAF + DDoS Std | TLS termination at the FD endpoint (custom domain in production), your cert, anycast, WAF managed rules + **custom IP-allowlist rule** (see §4.3) |
| Session controller | **Session Broker** (Azure Container Apps, FastAPI) | Mints a per-browser routing cookie, allocates a sandbox, proxies WebSocket → noVNC. **No user authentication is performed here** — the SaaS itself authenticates the user inside the sandbox. |
| Per-browser sandbox | **Azure Container Instances** (custom kiosk image in our ACR) | One ACI per browser, private-IP-only on a delegated subnet, destroyed on logout. Custom 250 MB image: Debian 12-slim + Xvfb + fluxbox + x11vnc + websockify + bundled noVNC + Chromium `--kiosk --app=$SAAS_URL` |
| Egress | **NAT Gateway + Standard Public IP** (or `/29` Public IP Prefix) | Stable Azure egress IP visible to the SaaS |
| DNS | **Azure DNS Private Resolver** | Region-consistent DNS resolution for the sandbox subnet |
| Observability | **Log Analytics** + Front Door / ACA diagnostics | Audit + troubleshooting |

**Region:** Pick an Azure region appropriate to the customer's data-residency, latency, and SaaS-allowlisting needs. The egress region does **not** have to match the user region; in many cloaking deployments it deliberately doesn't.

> **Why ACI instead of ACA Dynamic Sessions?** Dynamic Sessions disallows the privileged-mode operations a kiosk container needs (its own X server / init). We pivoted to per-browser Azure Container Instances on a delegated subnet inside the same VNet, orchestrated by the broker via ARM REST. See [docs/CUSTOMER-BRIEF.md §4](docs/CUSTOMER-BRIEF.md) for the full rationale.

### How a session flows

1. User opens the Front Door endpoint. The FD WAF first evaluates the **IP allowlist custom rule** (§2.3); requests from non-listed source IPs are blocked at the edge.
2. Allowed traffic is forwarded to the Session Broker via Private Link to the ACA env.
3. The broker checks for a `cloak_browser_id` cookie. If none, it mints a UUID and a signed `cloak_session` cookie — these are routing keys only, not auth credentials.
4. The broker pops a pre-warmed ACI from the **warm pool** (or provisions a new one) and pins it to that browser's cookie.
5. The broker returns an HTML page that opens a noVNC client; the client opens a **WebSocket** at `wss://<fd>/websockify` which the broker proxies to the sandbox ACI's websockify endpoint.
6. Inside the sandbox, Chromium `--kiosk --app=$SAAS_URL` is already loaded against the real SaaS. **The user authenticates against the SaaS here**, with whatever the SaaS demands (Entra, Okta, username+password, MFA, passkey — the SaaS owns this flow). The broker never sees these credentials.
7. SaaS sees the **NAT Gateway public IP** (Azure-owned, in the chosen egress region) — never the corp IP, never the user.
8. On logout / idle / timeout / WS close the broker calls ARM `DELETE` on the ACI; the warmer provisions a replacement. Next session = fresh ACI = fresh SaaS login.

### 2.1 Session Broker — why it has to exist

The Session Broker is **not** an Azure product. It's a small custom service ([broker/](broker/)) hosted as a regular Azure Container App (1 container image, 1–5 replicas behind ACA's internal ingress) and is the only thing Azure Front Door exposes publicly.

Azure Container Instances offers a REST API to spin up / destroy a single-tenant Hyper-V isolated container per call, but it does **not** know:

- *which* sandbox belongs to which browser session
- how to hand the user a one-time URL to attach to their sandbox
- when to destroy the sandbox
- how to keep an idle warm pool to hide the ~60 s cold-start cost

The broker fills that gap. Without it, there is no per-browser mapping, no warm pool, and no lifecycle control over the sandbox fleet.

### 2.2 Session Broker — responsibilities

1. **Terminate the user session** — receives the HTTPS / WSS request from Front Door over Private Link.
2. **Identify the browser** — mint or read an HttpOnly `cloak_browser_id` UUID + a signed `cloak_session` cookie. **This is a routing key, not an authentication credential.** All real authentication happens between the user and the SaaS *inside* the sandbox.
3. **Allocate a sandbox** — pop one ACI from the warm pool (`_idle_pool`); if empty, create one synchronously via ARM `PUT .../containerGroups/{name}` using the broker's managed identity. Map `(browserId → sandboxName)` so repeat visits from the same browser reuse their sandbox.
4. **Return the attach page** — broker emits an HTML page containing a noVNC iframe pointing at `/vnc.html?autoconnect=1&resize=remote&path=websockify`.
5. **Proxy the WebSocket** — the noVNC client opens `wss://<fd>/websockify`. Broker checks the `cloak_session` cookie, then proxies frames to the claimed ACI's `:6901/websockify` endpoint.
6. **Track lifecycle** — on logout / idle timeout / WS close, broker calls ARM `DELETE` on the container group; warmer loop provisions a replacement to keep the pool at `WARM_POOL_SIZE`.
7. **Emit audit logs** — `browserId → sandboxName → start/stop time → egress IP` to Log Analytics.

**Why a Container App and not a Function or VM:**

| Need | Why ACA Container App fits |
|---|---|
| Long-lived WebSocket per user (pixel stream) | ACA supports WebSockets natively; Functions handle them poorly |
| Private VNet + Private Link from Front Door | ACA internal ingress supports both |
| Scale 2 → 5 replicas on concurrent sessions | KEDA HTTP scaler |
| Stateless, easy redeploy | Container image, no host to patch |
| Cheap when idle (≈ 2 small replicas) | Per-second compute billing |

**What the broker is not:**

- Not a reverse proxy for the SaaS — SaaS traffic never touches the broker; it goes sandbox → NAT Gateway → SaaS.
- Not an identity provider — there is no broker-level login; the SaaS authenticates the user inside the sandbox.
- Not where SaaS responses are decoded or rewritten — that's exactly the model that failed for prior vendors.
- Not a state store — sandbox state lives in ACA; user identity lives in the SaaS.

In one line: **the broker is the sandbox-lifecycle controller that turns a fresh browser into a ready-to-stream sandbox and tears it down when the user leaves. Authentication is the SaaS's job, performed inside the sandbox.**

### 2.3 Front Door WAF — IP allowlist (access gate)

Because the broker performs no user authentication, the **only** gate that decides who can reach the cloak URL is the Front Door WAF. The Bicep deploys a custom WAF rule whose source IPs come from a single parameter:

```bicep
@description('Public source IP/CIDR allowlist for the WAF. Empty = no IP restriction.')
param allowedSourceIps array = []
```

Declared in [`infra/main.bicep`](infra/main.bicep) and consumed by [`infra/modules/front-door.bicep`](infra/modules/front-door.bicep). Behaviour:

| `allowedSourceIps` value | Resulting WAF behaviour |
|---|---|
| `[]` (default; current PoC value in [`main.bicepparam`](infra/main.bicepparam)) | No IP restriction. Anyone on the public Internet who knows the FD URL can reach the broker. Use only during PoC, when the URL itself is the secret. |
| `['1.2.3.4/32', '5.6.7.0/24', ...]` | A WAF custom rule (`AllowOnlyListedIps`, priority 100) blocks every request whose `SocketAddr` is **not** in the list. The managed Default Rule Set + Bot Manager still apply on top. |

For production, fill the array with the customer's corporate egress CIDRs (e.g. their on-prem NAT IPs, their ZTNA / SASE egress IPs, their VPN concentrators). The WAF evaluates this rule at the FD edge, before any traffic reaches the broker or the ACA env, so blocked traffic incurs no compute cost.

To update the allowlist post-deploy without a full Bicep run:

```bash
az network front-door waf-policy rule update \
  -g <rg> --policy-name wafcloak<...> \
  -n AllowOnlyListedIps \
  --match-condition SocketAddr IPMatch 1.2.3.4/32 5.6.7.0/24 --negate true
```

---

## 3. Open-source components used

No browser-isolation product is purchased. The sandbox is assembled from well-known, audited open-source pieces; the broker is a small custom FastAPI service that orchestrates them. Everything runs inside the customer's Azure subscription.

| Component | Role | Why this one |
|---|---|---|
| **Debian 12-slim** | Sandbox base OS | Smallest mainstream Linux image (~80 MB) with a current security-supported Chromium build |
| **Chromium** (`chromium` Debian package) | The actual browser the SaaS sees | Same engine as Chrome; we don't need Chrome's proprietary codecs. Launched with `--kiosk --app=$SAAS_URL` so there's no address bar, tabs, or menus |
| **Xvfb** (X Virtual Framebuffer) | Headless display server inside the ACI | Renders Chromium's window into RAM with no GPU/console; standard for unattended Linux GUI apps |
| **fluxbox** | Minimal window manager | Just enough to give Chromium a parent window; ~1 MB; no taskbar, no system tray |
| **x11vnc** | VNC server reading Xvfb's framebuffer | Streams the framebuffer as VNC; flag `-nopw -localhost` keeps it bound to 127.0.0.1 inside the ACI so the only way in is via websockify, no password on the wire |
| **websockify** | Translates VNC↔WebSocket | Bridges raw VNC (TCP) into a WebSocket the broker can proxy through Front Door without a TCP tunnel |
| **noVNC** | HTML5 VNC client (JavaScript + `<canvas>`) | Lets the user attach with a stock browser — no plugin, no native VNC client, no extension. Bundled inside the kiosk image so it ships with the sandbox |
| **FastAPI** + **uvicorn** | Broker HTTP/WS framework | Native async, first-class WebSocket support (needed for the pixel-stream proxy), small image |
| **httpx** | ARM REST client inside the broker | Async; lets the broker provision/destroy ACIs and warm-pool replacements without blocking the request loop |
| **azure-identity** | Managed-identity token provider | Broker authenticates to ARM using its own ACA managed identity — no client secrets in code or env |
| **itsdangerous** | Signed routing-cookie helper | Mints/verifies the `cloak_session` cookie; not an auth credential, just a tamper-proof browser-routing key |

What we deliberately **did not use**:

- **Kasm Workspaces** — full desktop-streaming product. Required privileged-mode operations that ACA Dynamic Sessions blocks, image is 1.6 GB, and the Kasm login UI itself becomes pixel-level surface that has to be re-skinned to avoid leaking *"this is Kasm"*. Replaced by the 250 MB custom kiosk image.
- **Guacamole** — heavier (Java + servlet container + guacd) and oriented toward multi-protocol gateways (RDP/SSH/VNC). For a single-protocol noVNC bridge it adds a JVM and a separate daemon for no benefit.
- **WebRTC streamers (Selkies, Neko)** — give better latency on graphics-heavy apps, but pull in GStreamer + a DTLS/SRTP stack; for a SaaS workflow (mostly text and form fields) the VNC framebuffer is plenty and the attack surface is much smaller.

---

## 4. What the user can and cannot see — the cloaking promise

Verified against a 77-entry HAR captured from the live deployment. Full side-by-side comparison: [docs/F12-cloaking-test.md](docs/F12-cloaking-test.md).

| Vector | What user sees | Cloaked? |
|---|---|---|
| Browser URL bar | Front Door endpoint only (`*.azurefd.net` or custom domain) | ✅ |
| TLS certificate | Microsoft-issued AFD cert (or your custom cert) | ✅ |
| DevTools → Network tab | Only requests to the Front Door endpoint (0/77 to the SaaS); 1 long-lived WebSocket carrying VNC framebuffer frames | ✅ |
| HTTP response headers | Only broker + FD headers. `server`, `x-powered-by`, `x-nextjs-*`, `etag`, origin `cache-control`, `vary: rsc,next-router-…` — all absent | ✅ |
| DevTools → DOM / Elements | noVNC client + a single `<canvas>`; no SaaS markup | ✅ |
| `View Source` | noVNC client HTML, no SaaS markup | ✅ |
| Browser cookies / IndexedDB / localStorage | Only broker session cookie + `cloak_browser_id` UUID; nothing from the SaaS | ✅ |
| `nslookup` / OS DNS cache | Only the FD endpoint hostname | ✅ |
| Wireshark / `tcpdump` on user's laptop | Only TLS to Front Door anycast IPs | ✅ |
| Process memory of user's browser | noVNC state only — no SaaS HTML/JS/tokens | ✅ |
| OS-level packet capture | Same as Wireshark — Front Door only | ✅ |
| Cert-error interstitial leaking SaaS hostname inside the pixels | Suppressed via `INSECURE_SAAS=1` (scoped to `$SAAS_URL`) | ✅ |
| **Rendered pixels of the SaaS UI** | **The actual SaaS UI** — logos, layout, colors, data | ❌ — out of scope (R3) |
| Screenshot the screen | The SaaS UI is captured | ❌ |
| OCR a screenshot | Tenant name in the rendered text could be extracted | ❌ |

The cloaking promise is **network/DOM/storage layer**, not visual.

---

## 5. Constraints — what this design does **NOT** solve

This list must be reviewed with the customer. If any of these become must-haves, the design changes (or becomes infeasible).

### 5.1 Visual / pixel-level cloaking
- ❌ The user **will see the SaaS's UI** — branding, logos, color scheme, layout, data.
- ❌ A screenshot or screen recording captures the SaaS UI.
- ❌ OCR on a screenshot can extract any text rendered on screen, including a tenant name displayed by the SaaS.
- **Why:** No technology can render a SaaS's pages to a user *and* hide what those pages look like. The only solutions are (a) get an API from the SaaS and build a custom UI on top — separate project, separate path; or (b) accept the limitation.

### 5.2 SaaS features incompatible with browser-in-a-browser

The sandbox runs a real Chromium against the real SaaS, so cookies, third-party cookies, Service Workers, OAuth redirects, and standard MFA codes all work. The features that genuinely break are the ones that bind to **the user's local OS or device** — those signals never leave the user's laptop, so the SaaS-side check fails.

| Pattern | Why it breaks inside RBI |
|---|---|
| **WebAuthn with platform authenticator** (Touch ID, Windows Hello, OS-bound passkeys, Smart Card) | The credential lives on the user's laptop; the WebAuthn request is issued by the sandbox's Chromium, with a different origin/RP-ID and no path to the user's TPM/Secure Enclave. **Mitigation:** use a roaming USB security key forwarded into the sandbox, an authenticator-app TOTP, or push-based MFA — all of these work fine |
| **Conditional Access / device-trust policies** that require a managed device, a corp certificate, or a device-compliance signal | The sandbox is not the user's device. **Mitigation:** scope Conditional Access to the egress NAT IP / a service principal, or accept that users will be re-prompted for MFA each session |
| **Apps requiring browser extensions** (PGP, password managers, custom plugins) | The kiosk Chromium ships with no extensions and we keep it that way to preserve a uniform fingerprint |
| **OS integrations** (smartcard middleware, certificate stores, native messaging hosts) | Sandbox OS is not the user's OS. File pickers up to 100 MB/file are supported via the broker-mediated `/upload` endpoint — see [docs/UPLOAD.md](docs/UPLOAD.md) |
| **"Remember this device" cookies / persistent device trust** | Sandbox is destroyed at logout — fresh fingerprint every session. Expect frequent re-verification challenges |
| **Latency-sensitive or graphics-heavy apps** (video editors, 3D, real-time games, smooth-scroll dashboards with 30+ FPS animation) | RBI adds 60–100 ms input lag; not for these workloads |
| **Aggressive bot detection** (Akamai BMP, PerimeterX, Cloudflare Bot Management) | Fresh Azure IP + uniform fingerprint per session can trip bot scoring. **Mitigation:** allowlist the NAT Gateway egress IP with the SaaS as a trusted source |
| **Native clients** (Outlook desktop, Teams desktop, IMAP, SFTP) | Out of scope — RBI is browser-only by definition |
| **Mobile / offline access** | Out of scope by definition |

> **Action required from customer:** Confirm with a 30-min PoC on the actual target SaaS *before* committing to the full build. If the target SaaS depends on any row above for a critical flow, revisit.

### 5.3 Adversary capability ceiling

| Adversary capability | Defended? |
|---|---|
| DevTools, F12, View Source on user's browser | ✅ |
| Wireshark / tcpdump on user's laptop | ✅ |
| Browser memory dump of user's browser | ✅ (only streamer state present) |
| Forensic disk capture of user's laptop | ✅ for SaaS data (none persisted locally); ❌ if user took a screenshot |
| **Screen recording / screenshot tools on user's laptop** | ❌ — pixel data is by definition visible to the user; cannot be defeated |
| **OCR on screenshots** | ❌ |
| **Photographing the screen with a phone** | ❌ — physically unsolvable by any software |
| Coercing the user (rubber-hose) | ❌ — out of scope of any tech control |
| User opening the real SaaS directly in another tab on the same laptop | ❌ — needs **endpoint policy** outside this design (Intune/MDM URL block on `*.saas.com`), or move the user to a managed device (Windows 365 / AVD) |
| Sandbox compromise & lateral movement to other tenants | ✅ — Hyper-V isolation per session, no shared state, no persistent volume; this is the explicit fix for the customer's previous RBI bleed bug |
| Compromise of the egress NAT IP reputation | ⚠️ — if SaaS rate-limits or flags the IP, all 50 users impacted simultaneously; mitigation = `/29` Public IP Prefix + rotation |

### 5.4 Operational constraints
- ❌ No persistent per-user profile inside the sandbox — by design. Means every session starts fresh, with re-login, re-MFA, re-device-trust challenges. **Adding persistence reintroduces the cross-user contamination risk** the customer's old RBI had. Do not relax this.
- ❌ User cannot bookmark deep-links into the SaaS — only the cloak's entry URL.
- ❌ No download of SaaS-issued OAuth tokens / API keys to the user's device — these would be visible inside the sandbox browser only.
- ⚠️ Cost is dominated by **session-second** billing of Dynamic Sessions. Idle sessions burn money — aggressive idle-timeout in streamer + broker-side reaper required.
- ⚠️ ACA Dynamic Sessions custom-container pools — verify regional availability and image-size limits at PoC time. Service is relatively new.
- ⚠️ Front Door + WebSocket through Private Link to ACA internal ingress — confirm WS upgrade behavior at PoC. If problematic, fall back to App Gateway v2 in front of ACA (Front Door still preferred for public surface; AppGW only if WS is fragile).

### 5.5 Compliance / legal
- ❌ This design does not establish whether using the SaaS via RBI complies with the SaaS's ToS for *all* features. Customer asserted "ToS allows it" — get this in writing, ideally per-feature.
- ❌ Data-residency analysis is **not done**. SaaS traffic egresses from the chosen Azure region; if data is subject to a residency constraint that forbids that region, this design violates it. Customer must confirm.
- ❌ Audit trail of *what users did inside the SaaS* is limited to network metadata (Front Door logs, NAT GW flows). **Session recording** of the rendered pixels can be added (Kasm and similar support it) but adds storage cost and retention obligations.

---

## 6. Out-of-scope follow-ups (if customer requirements grow)

| If customer later asks for… | New design path |
|---|---|
| Hide visual branding too | **Path B** — requires SaaS API; build custom UI on App Service / Container Apps. Different project. |
| Defeat user on their own laptop (memory, screen recording) | **Path C** — move workflow to a device the user does not control: **Windows 365** or **AVD** with locked-down session host, no clipboard/print/screenshot, conditional access enforced. Even this does not defeat the "phone camera pointed at screen" attack. |
| Plain "hide corp public IP" (no SaaS-identity hiding) | **Path D** — VPN/ExpressRoute → AKS or VMSS with Envoy/Squid → NAT Gateway. Different stack entirely; Front Door would actually leak. Out of scope for this design. |

---

## 7. Files in this folder

| File | Purpose |
|---|---|
| `README.md` | This document |
| `topology.drawio` | Editable architecture diagram (open in draw.io) |
| `topology.png` | Rendered topology image referenced in §1 |
| `autoscale.drawio` | Editable autoscale diagram (broker replicas + per-browser sandbox lifecycle) |
| `autoscale.png` | Rendered autoscale image referenced in §1 |
| [`docs/CUSTOMER-BRIEF.md`](docs/CUSTOMER-BRIEF.md) | Customer-facing briefing: leak inventory, mitigation table, components, costs, lessons |
| [`docs/F12-cloaking-test.md`](docs/F12-cloaking-test.md) | Side-by-side DevTools/F12 comparison: direct-to-SaaS vs. via Cloak/AFD, with reproduction recipe |
| [`infra/`](infra/) | Subscription-scoped Bicep (network, FD + WAF, ACA env, broker, observability) |
| [`broker/`](broker/) | FastAPI session broker source (auth, pool, ARM client, websockify proxy) |
| [`sandbox/`](sandbox/) | Custom kiosk container (Dockerfile + entrypoint.sh) |
| [`scripts/`](scripts/) | `build-and-push.sh`, `deploy.sh`, `smoke-test.sh` |
