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
    consume_attach,
    destroy_sandbox,
    mint_attach_token,
)

logging.basicConfig(level=settings.broker_log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("broker")


@asynccontextmanager
async def lifespan(_: FastAPI):
    bag.load()
    logger.info("broker started; stub_auth=%s", settings.stub_auth)
    yield


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
<!-- Streamer attaches via WebSocket; the iframe loads the streamer's own client UI
     served by the sandbox over our WS proxy. The iframe src is same-origin, so
     F12 only sees portal.contoso.com. -->
<iframe src="/streamer.html?t=__TOKEN__"></iframe>
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
        # PoC: instant login as stub user.
        u = stub_user()
        resp = RedirectResponse("/session", status_code=303)
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
    rec = await allocate_sandbox(user.sub)
    token = mint_attach_token(rec)
    return HTMLResponse(ATTACH_HTML.replace("__TOKEN__", token))


# ---------- WebSocket proxy: user <-> sandbox ----------
@app.websocket("/attach")
async def attach_ws(ws: WebSocket, token: str = ""):
    rec = consume_attach(token)
    if not rec:
        await ws.close(code=4401)
        return
    await ws.accept()

    # Open an upstream WS to the sandbox via the Dynamic Sessions proxy URL.
    # Note: the Dynamic Sessions proxy supports HTTP CONNECT-style upgrades.
    import websockets

    auth_token = await _bearer_for_sandbox()
    upstream_url = rec.sandbox_url.replace("https://", "wss://").replace("http://", "ws://")

    try:
        async with websockets.connect(
            upstream_url,
            extra_headers={"Authorization": f"Bearer {auth_token}"},
            max_size=None,
        ) as upstream:
            async def pump_up():
                try:
                    while True:
                        data = await ws.receive_bytes()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass

            async def pump_down():
                try:
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)
                except Exception:  # noqa: BLE001
                    pass

            await asyncio.gather(pump_up(), pump_down())
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


async def _bearer_for_sandbox() -> str:
    # Reuse the same managed identity token used for sessions API.
    from .sessions import _bearer_token  # local import to avoid cycle
    return await _bearer_token()


# ---------- Streamer client passthrough ----------
# The Kasm-in-sandbox streamer serves its own JS/HTML on path `/`. We proxy minimal
# static assets through the broker so that the user's browser only ever talks to
# `portal.contoso.com`. For a true PoC we deliver a stub page that opens the WS;
# in production replace with a same-origin Kasm client bundle copied into /static.
STREAMER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Workspace</title>
<meta name="referrer" content="no-referrer">
<style>html,body{margin:0;height:100%;background:#000;color:#888;font-family:system-ui,sans-serif}
#log{position:fixed;bottom:8px;left:8px;font-size:11px;opacity:.6}</style>
</head><body>
<canvas id="screen" width="1920" height="1080" style="width:100%;height:100%;display:block"></canvas>
<div id="log">connecting...</div>
<script>
// Minimal stub. Replace with a real Kasm/noVNC client bundle for PoC.
const t = new URLSearchParams(location.search).get('t');
const ws = new WebSocket(`wss://${location.host}/attach?token=${encodeURIComponent(t)}`);
ws.binaryType = 'arraybuffer';
ws.onopen = () => document.getElementById('log').textContent = 'connected';
ws.onclose = () => document.getElementById('log').textContent = 'disconnected';
ws.onerror = () => document.getElementById('log').textContent = 'error';
</script></body></html>"""


@app.get("/streamer.html", response_class=HTMLResponse)
async def streamer(request: Request, t: str = ""):
    require_user(request)
    return HTMLResponse(STREAMER_HTML)
