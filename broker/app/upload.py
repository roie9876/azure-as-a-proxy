"""Broker-mediated upload endpoint.

User picks a file in their own browser → POSTs to /upload on the broker →
broker validates (size, MIME, per-session quota) → broker streams the bytes
to the claimed sandbox's file-inbox over the VNet → file lands in
~/uploads/ inside the kiosk container so Chromium's <input type="file">
picker can attach it.

The user never has shell access to the sandbox; this is the only way bytes
can flow user → sandbox. Bytes never flow back: download is blocked by
Chromium policy DownloadRestrictions=3 (see chromium-policies.json).

See docs/UPLOAD.md for the API contract.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, Request, UploadFile

from .config import settings
from .sessions import sandbox_for_user

logger = logging.getLogger("broker.upload")

# Per-browser-id rolling quota counter. Resets when the sandbox is destroyed
# (see destroy_sandbox in sessions.py — see _on_destroy hook below).
@dataclass
class _Quota:
    bytes_used: int = 0
    file_count: int = 0
    last_upload_at: float = field(default_factory=time.time)


_quotas: dict[str, _Quota] = {}
_quota_lock = asyncio.Lock()


def reset_quota(browser_id: str) -> None:
    """Called by sessions.destroy_sandbox so a new sandbox starts with a clean budget."""
    _quotas.pop(browser_id, None)


def _allowlist() -> set[str]:
    return {m.strip().lower() for m in settings.upload_mime_allowlist.split(",") if m.strip()}


# Reusable httpx client to push to sandboxes.
_inbox_client: httpx.AsyncClient | None = None


def _get_inbox_client() -> httpx.AsyncClient:
    global _inbox_client
    if _inbox_client is None:
        _inbox_client = httpx.AsyncClient(
            verify=False,
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=False,
            http2=False,
        )
    return _inbox_client


async def aclose() -> None:
    global _inbox_client
    if _inbox_client is not None:
        await _inbox_client.aclose()
        _inbox_client = None


async def handle_upload(request: Request, browser_id: str, file: UploadFile) -> dict:
    """Validate + forward an upload to this browser's sandbox file-inbox."""
    if not settings.upload_enabled:
        raise HTTPException(status_code=404, detail="upload disabled")

    # 1. Resolve the sandbox for this browser session.
    sb = sandbox_for_user(browser_id)
    if not sb or not sb.private_ip:
        raise HTTPException(status_code=409, detail="no sandbox; visit /session first")

    # 2. MIME allowlist (Content-Type as declared by the user agent).
    declared_mime = (file.content_type or "").split(";", 1)[0].strip().lower()
    allow = _allowlist()
    if declared_mime not in allow:
        logger.warning("upload rejected: bad mime %s for browser=%s", declared_mime, browser_id[:8])
        raise HTTPException(status_code=415, detail=f"content-type '{declared_mime}' not allowed")

    # 3. Size guard: prefer Content-Length on the multipart sub-part if known.
    declared_size = 0
    cl = request.headers.get("content-length")
    if cl:
        try:
            declared_size = int(cl)
        except ValueError:
            declared_size = 0
    if declared_size and declared_size > settings.upload_max_bytes + 64 * 1024:
        # +64K slack for multipart envelope. The real per-file enforcement is
        # the streaming check below; this is a fast pre-flight reject.
        raise HTTPException(status_code=413, detail="file exceeds per-file cap")

    # 4. Stream-read the file body, hashing + counting bytes.
    h = hashlib.sha256()
    written = 0
    chunks: list[bytes] = []
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > settings.upload_max_bytes:
            raise HTTPException(status_code=413, detail="file exceeds per-file cap")
        h.update(chunk)
        chunks.append(chunk)
    if written == 0:
        raise HTTPException(status_code=400, detail="empty file")

    # 5. Per-browser aggregate quota.
    async with _quota_lock:
        q = _quotas.setdefault(browser_id, _Quota())
        if q.bytes_used + written > settings.upload_session_max_bytes:
            raise HTTPException(status_code=413, detail="session upload quota exceeded")
        q.bytes_used += written
        q.file_count += 1
        q.last_upload_at = time.time()

    # 6. Forward to sandbox file-inbox.
    body = b"".join(chunks)
    safe_name = (file.filename or "upload.bin").replace("\x00", "")[:200]
    inbox_url = f"http://{sb.private_ip}:{settings.sandbox_inbox_port}/inbox"

    files = {"file": (safe_name, body, declared_mime)}
    headers = {}
    if settings.sandbox_inbox_token:
        headers["X-Inbox-Token"] = settings.sandbox_inbox_token

    client = _get_inbox_client()
    try:
        r = await client.post(inbox_url, files=files, headers=headers)
    except httpx.RequestError as ex:
        logger.error("inbox unreachable %s: %s", inbox_url, ex)
        # Roll back the quota since the bytes never landed.
        async with _quota_lock:
            q = _quotas.get(browser_id)
            if q:
                q.bytes_used = max(0, q.bytes_used - written)
                q.file_count = max(0, q.file_count - 1)
        raise HTTPException(status_code=502, detail="sandbox inbox unreachable") from ex

    if r.status_code >= 400:
        logger.warning(
            "inbox %s rejected upload size=%d sha256=%s status=%d body=%s",
            sb.name, written, h.hexdigest()[:12], r.status_code, r.text[:200],
        )
        raise HTTPException(status_code=502, detail=f"sandbox rejected upload ({r.status_code})")

    sha = h.hexdigest()
    # Audit line — picked up by Container App's stdout → Log Analytics.
    logger.info(
        "upload accepted browser=%s sandbox=%s name=%s mime=%s size=%d sha256=%s",
        browser_id[:8], sb.name, safe_name, declared_mime, written, sha,
    )
    return {
        "ok": True,
        "name": safe_name,
        "size": written,
        "sha256": sha,
        "mime": declared_mime,
        "session_used_bytes": _quotas[browser_id].bytes_used,
        "session_used_files": _quotas[browser_id].file_count,
    }
