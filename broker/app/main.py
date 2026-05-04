"""Session Broker — entrypoint.

Routes:
  GET  /                      Portal page (generic, no SaaS branding)
  GET  /healthz, /readyz      Health/readiness probes
  GET  /login                 Begin OIDC (or stub) login
  GET  /auth/callback         OIDC callback
  POST /session               Allocate a sandbox, redirect to /attach
  WS   /attach?token=...      Pixel-stream WebSocket proxy to the sandbox
  POST /logout                Tear down sandbox + clear cookie
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import (
    SESSION_COOKIE,
    STATE_COOKIE,
    AuthedUser,
    _oidc_metadata,
    begin_login_url,
    exchange_code,
    issue_session_cookie,
    random_state,
    read_session_cookie,
    require_user,
    stub_user,
)
from .config import settings
from .middleware import HeaderHygieneMiddleware
from .secrets_bag import bag
from .sessions import (
    allocate_sandbox,
    destroy_sandbox,
    sandbox_for_user,
    start_warmer,
    stop_warmer,
)

logging.basicConfig(level=settings.broker_log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("broker")


@asynccontextmanager
async def lifespan(_: FastAPI):
    bag.load()
    await start_warmer()
    logger.info("broker started; stub_auth=%s warm_pool_size=%d", settings.stub_auth, settings.warm_pool_size)
    try:
        yield
    finally:
        await stop_warmer()


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
PORTAL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Secure Workspace</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<style>
  body{font-family:system-ui,sans-serif;background:#0b0b0d;color:#e8e8ea;margin:0;display:grid;place-items:center;min-height:100vh}
  .card{background:#16161a;padding:32px 40px;border-radius:12px;max-width:420px;text-align:center;box-shadow:0 1px 0 rgba(255,255,255,.04)}
  h1{font-size:18px;margin:0 0 12px;font-weight:500}
  p{font-size:13px;color:#9aa0a6;line-height:1.5;margin:0 0 20px}
  button{background:#3b82f6;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:14px;cursor:pointer}
  button:hover{background:#2563eb}
</style>
</head>
<body>
  <div class="card">
    <h1>Secure Workspace</h1>
    <p>Sign in to start a session. Your session runs in an isolated environment and is destroyed when you log out.</p>
    <form action="/login" method="get"><button type="submit">Sign in</button></form>
  </div>
</body>
</html>"""

ATTACH_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Workspace</title>
<meta name="referrer" content="no-referrer">
<style>html,body,iframe{margin:0;padding:0;height:100%;width:100%;background:#000;border:0}</style>
</head>
<body>
<!-- noVNC client served by sandbox's websockify; reverse-proxied through the
     broker so the user's browser only ever talks to the cloak's domain.
     /websockify is the same-origin WS upgrade target. -->
<iframe src="/vnc.html?autoconnect=1&resize=remote&path=websockify"></iframe>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = read_session_cookie(request)
    if user:
        return RedirectResponse("/session", status_code=303)
    return HTMLResponse(PORTAL_HTML)


# ---------- Auth ----------
@app.get("/login")
async def login(request: Request):
    if settings.stub_auth:
        # PoC: per-browser identity. Each browser gets its own UUID cookie ->
        # its own sandbox. Two browsers = two ACIs = no cross-contamination
        # (README §3 / §4.2 step 2). Replaced when real OIDC is configured.
        import uuid
        browser_id = request.cookies.get("cloak_browser_id")
        if not browser_id:
            browser_id = uuid.uuid4().hex
        u = AuthedUser(sub=browser_id, email=f"{browser_id[:8]}@stub.local")
        resp = RedirectResponse("/session", status_code=303)
        resp.set_cookie(
            "cloak_browser_id", browser_id,
            httponly=True, secure=True, samesite="lax", path="/",
            max_age=60 * 60 * 8,  # 8h
        )
        resp.set_cookie(
            SESSION_COOKIE, issue_session_cookie(u),
            httponly=True, secure=True, samesite="lax", path="/",
            max_age=settings.session_idle_timeout_seconds,
        )
        return resp

    state = random_state()
    redirect_uri = str(request.url_for("auth_callback"))
    meta = await _oidc_metadata()
    url = begin_login_url(state, redirect_uri, meta)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(STATE_COOKIE, state, httponly=True, secure=True, samesite="lax", path="/", max_age=300)
    return resp


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    if settings.stub_auth:
        raise HTTPException(404)
    expected = request.cookies.get(STATE_COOKIE)
    if not expected or expected != state:
        raise HTTPException(400, "state mismatch")
    meta = await _oidc_metadata()
    redirect_uri = str(request.url_for("auth_callback"))
    user = await exchange_code(code, redirect_uri, meta)
    if not user.sub:
        raise HTTPException(401)
    resp = RedirectResponse("/session", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE, issue_session_cookie(user),
        httponly=True, secure=True, samesite="lax", path="/",
        max_age=settings.session_idle_timeout_seconds,
    )
    resp.delete_cookie(STATE_COOKIE, path="/")
    return resp


@app.post("/logout")
async def logout(request: Request):
    user = read_session_cookie(request)
    if user:
        await destroy_sandbox(user.sub)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------- Session allocation ----------
@app.get("/session", response_class=HTMLResponse)
async def session_page(request: Request):
    user = require_user(request)
    await allocate_sandbox(user.sub)
    return HTMLResponse(ATTACH_HTML)


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
    """Return the upstream base URL for the authenticated user's sandbox."""
    user = require_user(request)
    sb = sandbox_for_user(user.sub)
    if not sb or not sb.private_ip:
        # No sandbox yet — kick the user back to /session to allocate.
        raise HTTPException(status_code=409, detail="no sandbox; visit /session first")
    return f"{settings.sandbox_scheme}://{sb.private_ip}:{settings.sandbox_port}"


@app.websocket("/websockify")
async def websockify_proxy(ws: WebSocket):
    """Pixel-stream WebSocket: browser -> broker -> Kasm websockify."""
    # Validate session cookie BEFORE accept(); WebSocket carries cookies.
    user = read_session_cookie(ws)  # type: ignore[arg-type]
    if not user:
        await ws.close(code=4401)
        return
    sb = sandbox_for_user(user.sub)
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
    "", "healthz", "readyz", "login", "logout", "session", "auth/callback",
    "websockify", "favicon.ico",
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
