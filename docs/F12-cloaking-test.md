# F12 Visibility Test — Direct SaaS vs. via Cloak (AFD)

What an end user can extract from browser DevTools (Chrome F12 → Network) when
they reach the same SaaS application **directly** vs. **through the Cloak broker
behind Azure Front Door**.

- **Real SaaS origin (private):** `https://arh2b5deb8dmcvcf.fz37.alb.azure.com/` (Azure Application Gateway for Containers, self-signed cert, internal hostname)
- **Public face (what users hit):** `https://ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net/`

Test date: 2026-05-04. Source HAR: `~/Downloads/har-via-afd-v5.har` (77 entries captured during a full session).

---

## TL;DR

| Signal | Direct (no AFD) | Via Cloak/AFD | Hidden? |
|---|---|---|---|
| Origin hostname in URL bar | `arh2b5deb8dmcvcf.fz37.alb.azure.com` | `ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net` | ✅ |
| Origin hostname in any request URL | yes — every asset | never appears | ✅ |
| TLS cert (Subject/SAN) | `CN=arh2b5deb8dmcvcf.fz37.alb.azure.com` (self-signed) | Microsoft-issued AFD cert for `*.azurefd.net` | ✅ |
| `server:` header | `Microsoft-Azure-Application-LB/AGC` | *not present* | ✅ |
| `x-powered-by` | `Next.js` | *not present* | ✅ |
| `x-nextjs-*` headers | `cache: HIT`, `prerender: 1`, `stale-time: 300` | *not present* | ✅ |
| `etag` (origin-specific) | `"rteu81xptt4n0"` | *not present on dynamic resp.* | ✅ |
| `cache-control: s-maxage=31536000` | yes | *not present* | ✅ |
| `vary: rsc, next-router-state-tree, …` | yes (Next.js fingerprint) | *not present* | ✅ |
| Cert error in browser | YES — `NET::ERR_CERT_AUTHORITY_INVALID` | NO — clean Microsoft cert | ✅ |
| Response bodies (HTML/JS/CSS) | full Next.js HTML, hashed asset URLs reveal framework | only noVNC client + raster pixel frames over WebSocket | ✅ |
| Tech stack inferable | Next.js + AGC trivially | only "noVNC over Azure FD" | ✅ |

**Result:** every header, cert, URL and body that would identify the SaaS or its hosting platform is replaced by broker/AFD-only signals. The user never sees the destination origin in any form accessible through F12.

---

## 1. URL bar / network panel — direct

Hitting `https://arh2b5deb8dmcvcf.fz37.alb.azure.com/` directly:

- Browser shows the full SaaS hostname in the URL bar.
- TLS cert is self-signed → red "Your connection is not private" interstitial; user must click *Advanced → Proceed*. The error page itself displays `arh2b5deb8dmcvcf.fz37.alb.azure.com` in bold. *(see screenshot in earlier session)*
- Once accepted, every asset URL contains the SaaS hostname.

```
$ curl -sk -I https://arh2b5deb8dmcvcf.fz37.alb.azure.com/
HTTP/2 200
vary: rsc, next-router-state-tree, next-router-prefetch, next-router-segment-prefetch, Accept-Encoding
x-nextjs-cache: HIT
x-nextjs-prerender: 1
x-nextjs-stale-time: 300
x-powered-by: Next.js
cache-control: s-maxage=31536000
etag: "rteu81xptt4n0"
content-type: text/html; charset=utf-8
content-length: 6012
server: Microsoft-Azure-Application-LB/AGC
```

```
$ openssl s_client -connect arh2b5deb8dmcvcf.fz37.alb.azure.com:443 -servername arh2b5deb8dmcvcf.fz37.alb.azure.com
subject = CN=arh2b5deb8dmcvcf.fz37.alb.azure.com
issuer  = CN=arh2b5deb8dmcvcf.fz37.alb.azure.com   ← self-signed
X509v3 Subject Alternative Name:
    DNS:arh2b5deb8dmcvcf.fz37.alb.azure.com
```

**What the user learns from F12 in 5 seconds:**
- exact private hostname,
- it's Next.js,
- behind Azure Application Load Balancer for Containers (AGC),
- the cert isn't trusted (a phishing-y signal that frequently breaks user trust).

---

## 2. URL bar / network panel — via Cloak/AFD

Hitting `https://ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net/`:

- URL bar shows only the AFD endpoint. Padlock is green (Microsoft-issued cert).
- HAR captured 77 requests across one full kiosk session.

### Every URL in 77-entry HAR

```
ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net  ←  77 / 77 requests
arh2b5deb8dmcvcf.fz37.alb.azure.com        ←   0 / 77
fz37 / alb.azure.com                       ←   0 / 77
internal ACA defaultDomain                 ←   0 / 77
```

Search command (executed on the HAR):

```python
needles = ['arh2b5deb8','fz37','alb.azure.com','cae-cloak','azurecontainerapps']
# Hits across all URLs, request headers, request bodies,
# response headers, response bodies:  → 0 across the board.
```

### Every response header seen across 77 entries

| Header | Source | Reveals SaaS? |
|---|---|---|
| `date` | FD platform | no |
| `x-azure-ref` | FD platform | no (only "this passed through Azure FD") |
| `x-cache` | FD platform | no |
| `content-type`, `content-length`, `accept-ranges` | broker / static files | no |
| `content-security-policy: default-src 'self'…` | broker (noVNC page) | no — locks iframe to broker origin |
| `x-frame-options: SAMEORIGIN` | broker | no |
| `x-content-type-options: nosniff` | broker | no |
| `referrer-policy: no-referrer` | broker | no |
| `permissions-policy: camera=(), microphone=(), geolocation=(), interest-cohort=()` | broker | no |
| `last-modified: Fri, 22 Oct 2021 08:40:13 GMT` | mtime of bundled noVNC files inside the broker container | no — that's noVNC v1.3.0's release date, not the SaaS |
| `upgrade`, `connection`, `sec-websocket-accept`, `sec-websocket-extensions` | broker (the 1× WebSocket upgrade for `/websockify`) | no |

**Headers that would normally betray the SaaS but are absent in the HAR:**

- `server:` (no value at all → user can't even tell the broker is FastAPI)
- `x-powered-by:` — gone
- `x-nextjs-cache`, `x-nextjs-prerender`, `x-nextjs-stale-time` — gone
- `etag:` (origin-specific) — gone
- `cache-control: s-maxage=31536000` — gone
- `vary: rsc, next-router-state-tree, …` — gone

These are not stripped by a rule — they simply never reach the user, because the only HTTP response the user's browser ever sees comes from the broker (noVNC HTML/JS/CSS/images and the WebSocket upgrade). The SaaS response is consumed by Chromium **inside** the ACI.

### Response bodies

| Body type | Direct | Via Cloak |
|---|---|---|
| HTML | full Next.js page with hashed `_next/static/...` asset URLs | only `<iframe src="/vnc.html?...">` and noVNC's static `vnc.html` |
| JS | Next.js framework chunks | noVNC client (`app/error-handler.js`, etc.) |
| Images | SaaS app assets | only noVNC toolbar SVGs (`drag.svg`, `clipboard.svg`, `keyboard.svg`, …) |
| Real SaaS pixels | served as HTML/CSS | streamed as a binary RFB/VNC framebuffer over the single `wss://…/websockify` WebSocket — opaque to F12 |

The WebSocket frames in F12 are a stream of binary VNC protocol messages. There is no way to reverse-extract DOM, request headers, or responses from a rendered framebuffer.

### TLS cert

```
Issuer:  Microsoft (AFD-managed certificate for *.azurefd.net)
Subject: ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net
```

No cert warning. No mention of `arh2b5deb8…` anywhere in the chain.

---

## 3. What F12 can still see (residual, by design)

These are unavoidable Azure Front Door tells. They reveal "this site is fronted by Azure", **not** which SaaS or tenant sits behind it:

- `via:` (sometimes), `x-azure-ref`, `x-cache` headers
- `*.azurefd.net` hostname
- IP geolocation maps to Microsoft Edge POPs, not your SaaS region

If you want even those gone, put a **custom domain** (e.g. `app.example.com`) on the AFD endpoint. The user then sees only `app.example.com` and a cert you control; the `x-azure-ref` header is still set but doesn't identify *you*.

---

## 4. What F12 can see in addition to URLs/headers

| Thing | Direct | Via Cloak |
|---|---|---|
| WebSocket message framing | n/a | binary frames, ~30–60 fps, sizes vary with screen activity |
| Long-lived connections | many short HTTPS GETs | 1× WebSocket open for the session duration |
| Request rate | typical SPA | only static-asset bursts at session start; then nothing but WS traffic |
| `cookie` from SaaS | yes (Next.js session cookie if present) | never set on user's browser — lives only inside the kiosk Chromium ACI |

---

## 5. Reproduction (10 minutes)

1. Open Chrome → DevTools (F12) → Network tab → check *Preserve log* and *Disable cache*.
2. Navigate to `https://arh2b5deb8dmcvcf.fz37.alb.azure.com/`. Accept the cert warning. Browse for 30 s. Right-click → *Save all as HAR with content*.
3. Hard-refresh, navigate to `https://ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net/`. Browse the same SaaS for 30 s. Save HAR.
4. Diff the two HARs:

```bash
python3 -c "
import json
for label, path in [('direct','direct.har'), ('cloak','cloak.har')]:
    h = json.load(open(path))
    hosts = {e['request']['url'].split('/')[2] for e in h['log']['entries']}
    print(label, '->', hosts)
"
```

Expected:

```
direct -> {'arh2b5deb8dmcvcf.fz37.alb.azure.com'}
cloak  -> {'ep-cloak-f8fvdsf2eqd7gthx.b02.azurefd.net'}
```

That single-line difference is the entire cloaking guarantee, observable from any user's browser.
