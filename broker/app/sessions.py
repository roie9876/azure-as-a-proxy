"""Sandbox lifecycle: allocate a Dynamic Sessions instance, mint an attach token,
proxy the WebSocket between user and sandbox.

Attach tokens are opaque random IDs (NOT JWTs that encode SaaS info). State is held
in-memory in the broker process. For HA across replicas, swap `_attach_store` for
Redis (e.g. Azure Cache for Redis) — left as TODO.
"""
from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from azure.identity import DefaultAzureCredential
from fastapi import HTTPException

from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class AttachRecord:
    user_sub: str
    session_id: str
    sandbox_url: str
    expires_at: float = field(default_factory=lambda: time.time() + 60)


_attach_store: dict[str, AttachRecord] = {}
_active_sessions: dict[str, str] = {}  # user_sub -> session_id

_cred = DefaultAzureCredential()
# Dynamic Sessions API audience.
_TOKEN_SCOPE = "https://dynamicsessions.io/.default"


async def _bearer_token() -> str:
    # azure-identity is sync; offload.
    loop = asyncio.get_running_loop()
    tok = await loop.run_in_executor(None, lambda: _cred.get_token(_TOKEN_SCOPE))
    return tok.token


async def allocate_sandbox(user_sub: str) -> AttachRecord:
    """Allocate (or reuse) a Dynamic Sessions sandbox for this user, mint attach token."""
    if not settings.session_pool_endpoint:
        raise HTTPException(status_code=500, detail="session pool not configured")

    session_id = _active_sessions.get(user_sub) or f"u-{user_sub[:8]}-{_secrets.token_hex(4)}"
    token = await _bearer_token()
    api_version = "2025-02-02-preview"

    # Dynamic Sessions: allocate is implicit on first request to /code/execute|/proxy.
    # For Kasm we just want the session URL. The pool exposes a per-session base URL via:
    #   GET {poolManagementEndpoint}/sessions/{identifier}?api-version=...
    # If the session does not exist, calling any session-scoped endpoint creates it.
    base = settings.session_pool_endpoint.rstrip("/")
    url = f"{base}/sessions/{session_id}?api-version={api_version}"

    async with httpx.AsyncClient(timeout=15.0) as c:
        r = await c.get(url, headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (200, 201):
            data = r.json()
        elif r.status_code == 404:
            # Trigger creation via the proxy endpoint (any HTTP request to the session forces alloc).
            create_url = f"{base}/sessions/{session_id}/proxy?api-version={api_version}"
            r2 = await c.get(create_url, headers={"Authorization": f"Bearer {token}"})
            if r2.status_code >= 500:
                logger.error("sandbox alloc failed: %s %s", r2.status_code, r2.text)
                raise HTTPException(status_code=502, detail="sandbox allocation failed")
            data = {"identifier": session_id}
        else:
            logger.error("sessions API error: %s %s", r.status_code, r.text)
            raise HTTPException(status_code=502, detail="sessions api error")

    sandbox_url = f"{base}/sessions/{session_id}/proxy?api-version={api_version}"
    _active_sessions[user_sub] = session_id

    attach = _secrets.token_urlsafe(32)
    rec = AttachRecord(
        user_sub=user_sub,
        session_id=session_id,
        sandbox_url=sandbox_url,
        expires_at=time.time() + settings.attach_token_ttl_seconds,
    )
    _attach_store[attach] = rec
    return rec


def consume_attach(token: str) -> Optional[AttachRecord]:
    rec = _attach_store.pop(token, None)
    if rec and rec.expires_at < time.time():
        return None
    return rec


def mint_attach_token(rec: AttachRecord) -> str:
    """Re-add a record under a new token (for client redirect)."""
    tok = _secrets.token_urlsafe(32)
    _attach_store[tok] = rec
    return tok


async def destroy_sandbox(user_sub: str) -> None:
    session_id = _active_sessions.pop(user_sub, None)
    if not session_id:
        return
    token = await _bearer_token()
    api_version = "2025-10-02-preview"
    base = settings.session_pool_endpoint.rstrip("/")
    stop_url = f"{base}/.management/stopSession?api-version={api_version}&identifier={session_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            await c.post(stop_url, headers={"Authorization": f"Bearer {token}"})
    except Exception as ex:  # noqa: BLE001
        logger.warning("stopSession failed for %s: %s", session_id, ex)
