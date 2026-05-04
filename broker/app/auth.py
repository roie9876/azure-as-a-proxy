"""Browser-session cookie helpers.

There is **no user authentication** in the broker. The SaaS itself authenticates
the human inside the per-browser ACI sandbox.

The cookie below is purely a *routing key* so the broker can map an incoming
HTTP/WebSocket request to the correct ACI sandbox (1 browser = 1 sandbox).
It is signed with a deployment-stable secret (BROKER_SESSION_SECRET, injected
via ACA secrets) so all replicas can verify each other's cookies. If the env
var is missing we fall back to a per-process random secret — fine for local
dev / single-replica, but multi-replica deployments MUST set the env var or
~50% of cookie verifications will fail under round-robin load balancing.
"""
from __future__ import annotations

import os
import secrets as _secrets
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import settings

SESSION_COOKIE = "cloak_session"
BROWSER_ID_COOKIE = "cloak_browser_id"

# Deployment-stable signing key shared across all broker replicas. Sourced
# from ACA secret `broker-session-secret` (Bicep: aca-broker.bicep). Falls
# back to a per-process random value only when the env var is unset.
_SESSION_SECRET = os.environ.get("BROKER_SESSION_SECRET") or _secrets.token_urlsafe(48)
_serializer = URLSafeTimedSerializer(_SESSION_SECRET, salt="browser-session")


@dataclass
class BrowserSession:
    """Represents a single browser tab/session. `sub` is just a routing key."""
    sub: str


def issue_session_cookie(s: BrowserSession) -> str:
    return _serializer.dumps({"sub": s.sub})


def read_session_cookie(request: Request) -> Optional[BrowserSession]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer.loads(raw, max_age=settings.session_idle_timeout_seconds)
    except BadSignature:
        return None
    return BrowserSession(sub=data["sub"])


def mint_browser_id() -> str:
    return uuid.uuid4().hex


def require_session(request: Request) -> BrowserSession:
    s = read_session_cookie(request)
    if not s:
        raise HTTPException(status_code=401, detail="no session cookie")
    return s
