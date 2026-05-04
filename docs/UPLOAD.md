# File upload (Path 2 — broker-mediated)

## Why this exists

Chromium inside the per-browser ACI sandbox runs as PID 1 with no shell exposed
to the user, no DevTools, no clipboard, no file manager. When the SaaS the user
is interacting with shows an `<input type="file">` picker, the picker walks
the **sandbox's** filesystem — which is empty (`~/uploads/` is created fresh
on container start). So the user has nothing to attach.

Path 2 closes that gap without giving the user shell access to the sandbox or
exposing the sandbox to the public internet. The user's own browser uploads
the file through the broker (same origin as the noVNC stream), the broker
validates and audits it, and forwards the bytes to a tiny inbox service
inside the user's claimed sandbox. The file lands in `~/uploads/` so the next
time the SaaS opens its picker, the user can attach it.

```
[user browser]  --multipart POST /upload-->  [broker]
                                                |
                                     validate (size, MIME, quota)
                                     hash (sha256), audit-log
                                                |
                                                v
[broker] --HTTP POST /inbox over VNet--> [sandbox file-inbox :6902]
                                                |
                                                v
                                     ~/uploads/<filename>
                                                |
                                  picked up by Chromium <input type="file">
```

Bytes never flow back: Chromium policy `DownloadRestrictions=3` blocks all
downloads from inside the sandbox, so the file path is one-way.

## API

### `POST /upload`

Authenticated by the `cloak_session` cookie (same routing cookie that scopes
the noVNC stream). The session must already have a claimed sandbox — i.e. the
user has visited `/session` at least once.

Request:

```http
POST /upload HTTP/1.1
Cookie: cloak_session=...
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="file"; filename="contract.pdf"
Content-Type: application/pdf

<bytes>
```

Response (success, `201 Created`):

```json
{
  "ok": true,
  "name": "contract.pdf",
  "size": 412317,
  "sha256": "f1a3...",
  "mime": "application/pdf",
  "session_used_bytes": 412317,
  "session_used_files": 1
}
```

Errors:

| status | reason |
|--------|--------|
| 401    | missing/invalid `cloak_session` cookie |
| 409    | no sandbox claimed yet (visit `/session` first) |
| 415    | declared `Content-Type` not in the allowlist |
| 413    | file > per-file cap, or session aggregate > per-session cap |
| 502    | sandbox file-inbox unreachable or rejected the upload |

## Limits & guards

| limit                       | default                  | knob                                       |
|-----------------------------|--------------------------|--------------------------------------------|
| Per-file size               | 100 MB                   | `UPLOAD_MAX_BYTES`                         |
| Per-session aggregate       | 500 MB                   | `UPLOAD_SESSION_MAX_BYTES`                 |
| MIME allowlist              | pdf, docx, xlsx, png/jpeg/webp/gif, txt/csv/md, zip, json/xml | `UPLOAD_MIME_ALLOWLIST` (comma-sep) |
| Filename sanitization       | `[^A-Za-z0-9._-]` → `_`, max 200 chars, no leading dot | hard-coded in `sandbox/file-inbox.py` |
| Sandbox inbox auth          | optional shared-secret header `X-Inbox-Token` | `SANDBOX_INBOX_TOKEN` (broker) + `INBOX_TOKEN` (sandbox env) |
| Front Door body inspection  | bodies > 128 KB pass through without WAF body-inspection (Microsoft_DefaultRuleSet 2.1 default); WAF still inspects URI/headers | adjust `policySettings.requestBodyCheck` if tighter inspection is required |

The 100 MB per-file cap is also the practical Front Door / ACA ingress body
limit — going higher requires a different ingest path (e.g. presigned blob).

The aggregate quota is held in-memory per `browserId`. It resets when the
session is destroyed (logout, idle timeout, or sandbox eviction). This is fine
for a single broker replica or for sticky sessions; for HA without sticky
sessions, swap the in-memory dict in `broker/app/upload.py` for Redis.

## Audit log

Every accepted upload emits one structured stdout line on the broker, picked
up by Container App's Log Analytics workspace:

```
upload accepted browser=4f1c2a8b sandbox=sbx-1739293021-9f3a name=contract.pdf mime=application/pdf size=412317 sha256=f1a3...
```

Query in Log Analytics:

```kusto
ContainerAppConsoleLogs_CL
| where Log_s contains "upload accepted"
| project TimeGenerated, Log_s
```

The sandbox file-inbox also logs the hash on receipt; cross-check both lines
to confirm bytes travelled end-to-end.

## Threat model notes

- **Browser-side spoofing of `Content-Type`**: the allowlist is enforced on the
  user-declared MIME, which is trivial to spoof. This is intentional — the
  allowlist is a coarse usability filter, not a security boundary. The
  security boundary is the sandbox itself: even if a user uploads an
  executable, the kiosk image has no shell exposed to them and Chromium can't
  execute arbitrary files. To tighten this, add server-side magic-byte
  sniffing in `broker/app/upload.py:handle_upload` before forwarding.
- **Antivirus**: not enabled in this PoC. Drop a ClamAV sidecar into
  `infra/modules/aca-broker.bicep` and POST each upload to it before forwarding
  if the deployment carries regulated data.
- **DoS**: ACA HTTP scale rule (`concurrentRequests=50`) bounds in-flight
  uploads per replica. Per-session quota bounds the per-user budget. NAT GW
  egress is unaffected by uploads (they don't egress).
- **Cross-tenant leak**: each upload is forwarded to **only** the sandbox
  claimed by the request's `browserId`. The lookup is a strict map — no
  fallback, no cross-tenant fan-out.
