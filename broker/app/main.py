"""Session Broker — entrypoint.

No user authentication. The SaaS itself authenticates the human inside the
per-browser ACI sandbox. The broker's cookie is a routing key, not a credential.

Routes:
  GET  /                      Mint browser-session cookie, redirect to /session
  GET  /healthz, /readyz      Health/readiness probes
  GET  /session               Allocate (or attach to) this browser's sandbox
  WS   /websockify            Pixel-stream WebSocket proxy to the sandbox
  POST /logout                Tear down sandbox + clear cookie
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import (
    BROWSER_ID_COOKIE,
    SESSION_COOKIE,
    BrowserSession,
    issue_session_cookie,
    mint_browser_id,
    read_session_cookie,
    require_session,
)
from .config import settings
from .middleware import HeaderHygieneMiddleware
from .sessions import (
    allocate_sandbox,
    destroy_sandbox,
    sandbox_for_user,
    start_warmer,
    stop_warmer,
)
from .upload import aclose as upload_aclose, handle_upload, reset_quota

logging.basicConfig(level=settings.broker_log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("broker")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await start_warmer()
    logger.info("broker started; warm_pool_size=%d", settings.warm_pool_size)
    try:
        yield
    finally:
        await stop_warmer()
        await upload_aclose()


app = FastAPI(title="cloak-broker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(HeaderHygieneMiddleware)


# ---------- Health ----------
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    return {"ok": True}


# ---------- Portal page ----------
# No portal/sign-in UI: the broker mints a routing cookie automatically.
# The user lands directly inside the per-browser ACI sandbox where the SaaS
# enforces its own authentication.

ATTACH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Workspace</title>
<meta name="referrer" content="no-referrer">
<style>
  html,body{margin:0;padding:0;height:100%;width:100%;background:#000;border:0;font-family:system-ui,sans-serif}
  iframe{position:absolute;inset:0;height:100%;width:100%;border:0;background:#000}
  #upbar{position:fixed;top:8px;right:8px;z-index:10;background:rgba(20,20,24,.88);
         color:#eaeaea;border:1px solid #333;border-radius:8px;padding:8px 12px;
         font-size:12px;display:flex;flex-direction:column;gap:6px;align-items:stretch;
         backdrop-filter:blur(6px);max-width:360px}
  #uphdr{display:flex;gap:8px;align-items:center;justify-content:space-between}
  #upbar button{background:#2a6df4;color:#fff;border:0;border-radius:5px;padding:6px 12px;
                font-size:12px;cursor:pointer;font-weight:600}
  #upbar button:disabled{opacity:.5;cursor:wait}
  #uphint{font-size:11px;color:#9aa3b2;line-height:1.35}
  #upmsg{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#7fd49a}
  #upmsg.err{color:#ff8a8a}
  #upbar.hidden{display:none}
</style>
</head>
<body>
<!-- noVNC client served by sandbox's websockify; reverse-proxied through the
     broker so the user's browser only ever talks to the cloak's domain.
     /websockify is the same-origin WS upgrade target. -->
<iframe src="/vnc.html?autoconnect=1&resize=remote&path=websockify"></iframe>

<div id="upbar" title="Send a file from your laptop into the sandbox so the SaaS file-picker can attach it. Files are saved under ~/uploads/ inside the sandbox.">
  <div id="uphdr">
    <strong>Send file to sandbox</strong>
    <button id="upbtn" type="button">Choose file…</button>
  </div>
  <div id="uphint">Use this when the SaaS asks you to attach a file. The file goes to <code>~/uploads/</code> inside the sandbox; then click the SaaS's own upload button and pick it from there.</div>
  <div id="upmsg">No file sent yet.</div>
  <input id="upfile" type="file" style="display:none">
</div>

<script>
(function(){
  const btn = document.getElementById('upbtn');
  const inp = document.getElementById('upfile');
  const msg = document.getElementById('upmsg');
  btn.addEventListener('click', () => inp.click());
  inp.addEventListener('change', async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    btn.disabled = true;
    msg.classList.remove('err');
    msg.textContent = 'Sending ' + f.name + '…';
    try {
      const fd = new FormData();
      fd.append('file', f, f.name);
      const r = await fetch('/upload', { method: 'POST', body: fd, credentials: 'same-origin' });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        msg.classList.add('err');
        msg.textContent = 'Failed: ' + (j.detail || r.status);
      } else {
        msg.textContent = '✓ ' + j.name + ' (' + Math.round(j.size/1024) + ' KB) → ~/uploads/';
      }
    } catch (e) {
      msg.classList.add('err');
      msg.textContent = 'Error: ' + e;
    } finally {
      btn.disabled = false;
      inp.value = '';
    }
  });
})();
</script>
</body>
</html>"""


def _ensure_session(request: Request) -> tuple[BrowserSession, RedirectResponse | None]:
    """Return (session, optional redirect-with-fresh-cookies).

    If the request has no valid cookie, mint a new browser_id, set both cookies
    on a 303 redirect to /session so the next hop carries them.
    """
    s = read_session_cookie(request)
    if s:
        return s, None
    browser_id = request.cookies.get(BROWSER_ID_COOKIE) or mint_browser_id()
    new_s = BrowserSession(sub=browser_id)
    resp = RedirectResponse("/session", status_code=303)
    resp.set_cookie(
        BROWSER_ID_COOKIE, browser_id,
        httponly=True, secure=True, samesite="lax", path="/",
        max_age=settings.browser_id_ttl_seconds,
    )
    resp.set_cookie(
        SESSION_COOKIE, issue_session_cookie(new_s),
        httponly=True, secure=True, samesite="lax", path="/",
        max_age=settings.session_idle_timeout_seconds,
    )
    return new_s, resp


@app.get("/")
async def root(request: Request):
    """Mint cookies on first visit, then bounce to /session."""
    _, redirect = _ensure_session(request)
    if redirect is not None:
        return redirect
    return RedirectResponse("/session", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    s = read_session_cookie(request)
    if s:
        await destroy_sandbox(s.sub)
        reset_quota(s.sub)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------- Session allocation ----------
@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request):
    s, redirect = _ensure_session(request)
    if redirect is not None:
        return redirect
    await allocate_sandbox(s.sub)
    return HTMLResponse(ATTACH_HTML)


# ---------- File upload (broker-mediated; see docs/UPLOAD.md) ----------
@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    s = require_session(request)
    result = await handle_upload(request, s.sub, file)
    return JSONResponse(result, status_code=201)


# ---------- Reverse proxy: broker -> Kasm sandbox (HTTP + WebSocket) ----------
# Kasm Chromium serves its noVNC UI on https://<aci-ip>:6901 (self-signed cert)
# and the websockify VNC stream at wss://<aci-ip>:6901/websockify.
# We expose both same-origin under the broker so the user's browser only ever
# talks to portal.contoso.com.

# Hop-by-hop headers we must NOT forward.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}


def _resolve_sandbox_base(request: Request) -> str:
    """Return the upstream base URL for this browser's sandbox."""
    s = require_session(request)
    sb = sandbox_for_user(s.sub)
    if not sb or not sb.private_ip:
        # No sandbox yet — kick the browser back to /session to allocate.
        raise HTTPException(status_code=409, detail="no sandbox; visit /session first")
    return f"{settings.sandbox_scheme}://{sb.private_ip}:{settings.sandbox_port}"


@app.websocket("/websockify")
async def websockify_proxy(ws: WebSocket):
    """Pixel-stream WebSocket: browser -> broker -> sandbox websockify."""
    # Validate routing cookie BEFORE accept(); WebSocket carries cookies.
    s = read_session_cookie(ws)  # type: ignore[arg-type]
    if not s:
        await ws.close(code=4401)
        return
    sb = sandbox_for_user(s.sub)
    if not sb or not sb.private_ip:
        await ws.close(code=4404)
        return

    upstream_url = (
        f"wss://{sb.private_ip}:{settings.sandbox_port}/websockify"
        if settings.sandbox_scheme == "https"
        else f"ws://{sb.private_ip}:{settings.sandbox_port}/websockify"
    )

    import ssl as _ssl
    import websockets

    ssl_ctx = _ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl.CERT_NONE

    # Echo only the subprotocol the client actually requested. noVNC modern
    # builds send NO subprotocols; older builds send 'binary'. Forcing 'binary'
    # when the client didn't ask causes the browser to close on protocol mismatch.
    requested_protocols = [
        p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",") if p.strip()
    ]
    chosen = "binary" if "binary" in requested_protocols else None
    upstream_subprotocols = [chosen] if chosen else None
    await ws.accept(subprotocol=chosen)

    try:
        async with websockets.connect(
            upstream_url,
            ssl=ssl_ctx if settings.sandbox_scheme == "https" else None,
            subprotocols=upstream_subprotocols,
            max_size=None,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
        ) as upstream:
            async def pump_up():
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                        elif "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                except WebSocketDisconnect:
                    return

            async def pump_down():
                try:
                    async for m in upstream:
                        if isinstance(m, bytes):
                            await ws.send_bytes(m)
                        else:
                            await ws.send_text(m)
                except Exception:  # noqa: BLE001
                    return

            done, pending = await asyncio.wait(
                {asyncio.create_task(pump_up()), asyncio.create_task(pump_down())},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as ex:  # noqa: BLE001
        logger.warning("websockify proxy error: %s", ex)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


# Reusable client w/ disabled TLS verification (Kasm self-signed).
_proxy_client: httpx.AsyncClient | None = None


def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            http2=False,
        )
    return _proxy_client


# Paths reserved by the broker — never proxied to the sandbox.
_BROKER_PATHS = {
    "", "healthz", "readyz", "logout", "session",
    "websockify", "favicon.ico", "upload",
}


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def reverse_proxy(path: str, request: Request):
    """Catchall HTTP reverse proxy → Kasm noVNC assets (vnc.html, app/, core/, ...)."""
    if path in _BROKER_PATHS:
        # Should have been matched by an explicit route already; if it gets here
        # that means the explicit route 404'd. Return 404 directly.
        raise HTTPException(status_code=404)

    base = _resolve_sandbox_base(request)
    upstream_url = f"{base}/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    # Kasm doesn't care about Host; let httpx set it from URL.
    fwd_headers.pop("host", None)

    body = await request.body()
    client = _get_proxy_client()

    try:
        upstream = await client.request(
            request.method,
            upstream_url,
            headers=fwd_headers,
            content=body if body else None,
        )
    except httpx.RequestError as ex:
        logger.warning("upstream proxy error %s: %s", upstream_url, ex)
        raise HTTPException(status_code=502, detail="sandbox unreachable") from ex

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )
