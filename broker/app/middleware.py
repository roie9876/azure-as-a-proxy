"""ASGI middleware that strips identity-leaking response headers.

This is defense in depth: Front Door's Rule Set already strips most of these,
but the broker should not emit them in the first place. F12 in the user's
browser will see only the headers we explicitly allow through.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

# Headers we forcibly remove on every response we emit.
STRIP_HEADERS = {
    b"server",
    b"x-powered-by",
    b"x-azure-ref",
    b"x-azure-fdid",
    b"x-cache",
    b"x-msedge-ref",
    b"x-fd-healthprobe",
    b"x-arr-loglevel",
    b"x-arr-ssl",
    b"x-original-url",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-proto",
    b"x-real-ip",
    b"x-client-ip",
    b"true-client-ip",
    b"via",
    b"x-request-id",
    b"x-correlation-id",
    b"x-amz-cf-id",
}

# Headers we always set (lowercase keys, values as bytes).
FORCE_HEADERS = {
    b"referrer-policy": b"no-referrer",
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"SAMEORIGIN",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), interest-cohort=()",
    # CSP: lock the portal page down. The /session page hosts a same-origin iframe
    # rendering /vnc.html (proxied from the sandbox), so frame-ancestors must allow 'self'.
    # noVNC ships inline <script> bootstrap, so script-src must include 'unsafe-inline'.
    b"content-security-policy": (
        b"default-src 'self'; "
        b"img-src 'self' data: blob:; "
        b"style-src 'self' 'unsafe-inline'; "
        b"script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        b"connect-src 'self' wss: ws:; "
        b"frame-src 'self'; "
        b"frame-ancestors 'self'; "
        b"form-action 'self'; "
        b"base-uri 'none'"
    ),
}


class HeaderHygieneMiddleware:
    """Strips fingerprint headers on responses + sets privacy defaults.

    Implemented at the raw ASGI layer so it works for HTTP responses *and*
    WebSocket upgrade handshakes (response headers on `websocket.accept`).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] in ("http.response.start", "websocket.accept"):
                headers = list(message.get("headers", []))
                # Drop offending headers
                headers = [(k, v) for (k, v) in headers if k.lower() not in STRIP_HEADERS]
                # Add forced headers (only on http responses; websocket.accept has limited use)
                if message["type"] == "http.response.start":
                    existing = {k.lower() for (k, _) in headers}
                    for k, v in FORCE_HEADERS.items():
                        if k not in existing:
                            headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
