"""Authentication: external OIDC redirect + allowlist check.

Two modes:
- Real OIDC (`OIDC_ISSUER` + `OIDC_CLIENT_ID` set, secret in Key Vault):
    Redirects to issuer for code flow. We use `authlib` for discovery + token exchange.
- Stub auth (PoC): a static "user" sub is granted (configurable). Use only for smoke tests.
"""
from __future__ import annotations

import logging
import secrets as _secrets
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import settings
from .secrets_bag import bag

logger = logging.getLogger(__name__)

STATE_COOKIE = "cloak_state"
SESSION_COOKIE = "cloak_session"

_user_serializer: URLSafeTimedSerializer | None = None


def _serializer() -> URLSafeTimedSerializer:
    global _user_serializer
    if _user_serializer is None:
        _user_serializer = URLSafeTimedSerializer(bag.session_secret, salt="user-session")
    return _user_serializer


@dataclass
class AuthedUser:
    sub: str
    email: Optional[str] = None


def _allowlist_ok(user: AuthedUser) -> bool:
    al = settings.allowlist_set
    if not al:
        # Empty allowlist == deny-all in non-stub mode; allow-all only in stub mode.
        return settings.stub_auth
    candidates = {user.sub.lower()}
    if user.email:
        candidates.add(user.email.lower())
    return bool(candidates & al)


def issue_session_cookie(user: AuthedUser) -> str:
    return _serializer().dumps({"sub": user.sub, "email": user.email})


def read_session_cookie(request: Request) -> Optional[AuthedUser]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _serializer().loads(raw, max_age=settings.session_idle_timeout_seconds)
    except BadSignature:
        return None
    return AuthedUser(sub=data["sub"], email=data.get("email"))


# ---------- OIDC ----------
async def _oidc_metadata() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration")
        r.raise_for_status()
        return r.json()


def begin_login_url(state: str, redirect_uri: str, meta: dict) -> str:
    from urllib.parse import urlencode
    qs = urlencode({
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "state": state,
    })
    return f"{meta['authorization_endpoint']}?{qs}"


async def exchange_code(code: str, redirect_uri: str, meta: dict) -> AuthedUser:
    if not bag.oidc_client_secret:
        raise HTTPException(status_code=500, detail="OIDC client secret missing")
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.oidc_client_id,
            "client_secret": bag.oidc_client_secret,
        })
        if r.status_code != 200:
            logger.warning("token endpoint failed: %s %s", r.status_code, r.text)
            raise HTTPException(status_code=401, detail="oidc token exchange failed")
        tok = r.json()

        # We trust the IdP's userinfo endpoint for sub/email; for stricter validation,
        # verify the id_token signature against jwks_uri (left as TODO).
        ui = await c.get(
            meta["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {tok['access_token']}"},
        )
        ui.raise_for_status()
        info = ui.json()
        return AuthedUser(sub=info.get("sub", ""), email=info.get("email"))


def random_state() -> str:
    return _secrets.token_urlsafe(24)


def require_user(request: Request) -> AuthedUser:
    user = read_session_cookie(request)
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    if not _allowlist_ok(user):
        raise HTTPException(status_code=403, detail="user not on allowlist")
    return user


# ---------- Stub (PoC only) ----------
def stub_user() -> AuthedUser:
    return AuthedUser(sub="stub-user", email="stub@local")
