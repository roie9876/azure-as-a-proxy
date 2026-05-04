"""Sandbox lifecycle: per-user Azure Container Instance (ACI) running Kasm Chromium.

Warm pool pattern:
- Background task keeps WARM_POOL_SIZE idle ACIs ready (created but unclaimed).
- On user login, an idle ACI is claimed and its private endpoint returned (sub-second).
- Replacement is spawned in the background to refill the pool.
- On logout/idle timeout, the ACI is deleted.

State is in-memory; for HA swap _idle_pool / _claimed for Redis. NAT GW handles egress
cloaking; sandbox subnet is delegated to Microsoft.ContainerInstance/containerGroups.
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

ARM_API_VERSION = "2023-05-01"
ARM_BASE = "https://management.azure.com"

_cred = DefaultAzureCredential()


async def _arm_token() -> str:
    loop = asyncio.get_running_loop()
    tok = await loop.run_in_executor(None, lambda: _cred.get_token("https://management.azure.com/.default"))
    return tok.token


def _aci_url(name: str) -> str:
    return (
        f"{ARM_BASE}/subscriptions/{settings.azure_subscription_id}"
        f"/resourceGroups/{settings.azure_resource_group}"
        f"/providers/Microsoft.ContainerInstance/containerGroups/{name}"
        f"?api-version={ARM_API_VERSION}"
    )


def _container_group_body(name: str) -> dict:
    body: dict = {
        "location": settings.azure_location,
        "tags": {"project": "saas-network-identity-cloak", "managedBy": "broker", "name": name},
        "properties": {
            "osType": "Linux",
            "restartPolicy": "OnFailure",
            "subnetIds": [{"id": settings.sandbox_subnet_id}] if settings.sandbox_subnet_id else [],
            "containers": [
                {
                    "name": "sandbox",
                    "properties": {
                        "image": settings.sandbox_image,
                        "resources": {"requests": {"cpu": 2.0, "memoryInGB": 4.0}},
                        "ports": [
                            {"protocol": "TCP", "port": settings.sandbox_port},
                            {"protocol": "TCP", "port": settings.sandbox_inbox_port},
                        ],
                        "environmentVariables": [
                            {"name": "SAAS_URL", "value": settings.saas_url},
                            {"name": "LANG", "value": "en_US.UTF-8"},
                            {"name": "TZ", "value": "Europe/Stockholm"},
                            {"name": "SCREEN_GEOMETRY", "value": "1920x1080x24"},
                            {"name": "INBOX_PORT", "value": str(settings.sandbox_inbox_port)},
                            {"name": "INBOX_TOKEN", "value": settings.sandbox_inbox_token},
                            {
                                "name": "BROKER_INBOX_MAX_BYTES",
                                "value": str(settings.upload_max_bytes),
                            },
                        ],
                    },
                }
            ],
            "ipAddress": {
                "type": "Private",
                "ports": [
                    {"protocol": "TCP", "port": settings.sandbox_port},
                    {"protocol": "TCP", "port": settings.sandbox_inbox_port},
                ],
            },
        },
    }
    if settings.acr_server and settings.acr_username and settings.acr_password:
        body["properties"]["imageRegistryCredentials"] = [
            {
                "server": settings.acr_server,
                "username": settings.acr_username,
                "password": settings.acr_password,
            }
        ]
    return body


@dataclass
class Sandbox:
    name: str
    private_ip: Optional[str] = None
    state: str = "Pending"  # Pending | Running | Claimed | Terminating
    claimed_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class AttachRecord:
    user_sub: str
    sandbox_name: str
    sandbox_url: str
    expires_at: float = field(default_factory=lambda: time.time() + 60)


# In-memory state
_idle_pool: dict[str, Sandbox] = {}      # name -> Sandbox (Running, unclaimed)
_pending: dict[str, Sandbox] = {}        # name -> Sandbox (still provisioning)
_claimed: dict[str, Sandbox] = {}        # name -> Sandbox (claimed by user)
_user_to_sandbox: dict[str, str] = {}    # user_sub -> sandbox name
_attach_store: dict[str, AttachRecord] = {}

_pool_lock = asyncio.Lock()
_warmer_task: Optional[asyncio.Task] = None


async def _arm_request(method: str, url: str, json_body: Optional[dict] = None) -> tuple[int, dict]:
    token = await _arm_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.request(method, url, headers=headers, json=json_body)
    try:
        data = r.json() if r.content else {}
    except Exception:  # noqa: BLE001
        data = {"raw": r.text}
    return r.status_code, data


async def _create_aci(name: str) -> Sandbox:
    sb = Sandbox(name=name, state="Pending")
    _pending[name] = sb
    body = _container_group_body(name)
    status, data = await _arm_request("PUT", _aci_url(name), body)
    if status >= 400:
        logger.error("ACI create %s failed: %s %s", name, status, data)
        _pending.pop(name, None)
        raise RuntimeError(f"ACI create failed: {status}")
    return sb


async def _poll_until_running(sb: Sandbox, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = await _arm_request("GET", _aci_url(sb.name))
        if status >= 400:
            logger.warning("poll %s -> %s", sb.name, status)
            await asyncio.sleep(3)
            continue
        props = data.get("properties", {})
        ig_state = props.get("instanceView", {}).get("state") or props.get("provisioningState")
        ip = props.get("ipAddress", {}).get("ip")
        if ip and ip != "0.0.0.0" and ig_state in ("Running", "Succeeded"):
            sb.private_ip = ip
            sb.state = "Running"
            return True
        if ig_state in ("Failed", "Canceled"):
            logger.error("ACI %s entered terminal state %s", sb.name, ig_state)
            return False
        await asyncio.sleep(3)
    return False


async def _delete_aci(name: str) -> None:
    try:
        await _arm_request("DELETE", _aci_url(name))
    except Exception as ex:  # noqa: BLE001
        logger.warning("ACI delete %s failed: %s", name, ex)


async def _provision_one_into_pool() -> None:
    name = f"sbx-{int(time.time())}-{_secrets.token_hex(3)}"
    try:
        sb = await _create_aci(name)
    except Exception as ex:  # noqa: BLE001
        logger.error("warm provision failed: %s", ex)
        return
    ok = await _poll_until_running(sb)
    _pending.pop(name, None)
    if not ok:
        await _delete_aci(name)
        return
    async with _pool_lock:
        if sb.claimed_by is None and name not in _claimed:
            _idle_pool[name] = sb
            logger.info("warm sandbox ready: %s ip=%s (idle pool size=%d)", name, sb.private_ip, len(_idle_pool))


async def _warmer_loop() -> None:
    """Keep _idle_pool at WARM_POOL_SIZE."""
    while True:
        try:
            async with _pool_lock:
                shortfall = settings.warm_pool_size - len(_idle_pool) - len(_pending)
            if shortfall > 0:
                await asyncio.gather(*[_provision_one_into_pool() for _ in range(shortfall)])
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001
            logger.exception("warmer loop error: %s", ex)
            await asyncio.sleep(10)


async def start_warmer() -> None:
    global _warmer_task
    if _warmer_task is None or _warmer_task.done():
        _warmer_task = asyncio.create_task(_warmer_loop(), name="sandbox-warmer")
        logger.info("sandbox warmer started (target=%d)", settings.warm_pool_size)


async def stop_warmer() -> None:
    global _warmer_task
    if _warmer_task:
        _warmer_task.cancel()
        try:
            await _warmer_task
        except asyncio.CancelledError:
            pass
        _warmer_task = None


async def allocate_sandbox(user_sub: str) -> AttachRecord:
    """Claim a warm sandbox; if pool empty, provision on demand and wait."""
    # Reuse if user already has one
    existing = _user_to_sandbox.get(user_sub)
    if existing and existing in _claimed:
        sb = _claimed[existing]
    else:
        async with _pool_lock:
            sb: Optional[Sandbox] = None
            if _idle_pool:
                name, sb = _idle_pool.popitem()
                sb.claimed_by = user_sub
                sb.state = "Claimed"
                _claimed[name] = sb
                _user_to_sandbox[user_sub] = name
        if sb is None:
            # Cold path: spin one up synchronously
            logger.info("idle pool empty, cold-provisioning for %s", user_sub)
            name = f"sbx-{int(time.time())}-{_secrets.token_hex(3)}"
            sb = await _create_aci(name)
            ok = await _poll_until_running(sb)
            _pending.pop(name, None)
            if not ok:
                await _delete_aci(name)
                raise HTTPException(status_code=502, detail="sandbox provisioning failed")
            sb.claimed_by = user_sub
            sb.state = "Claimed"
            _claimed[name] = sb
            _user_to_sandbox[user_sub] = name

    sandbox_url = f"{settings.sandbox_scheme}://{sb.private_ip}:{settings.sandbox_port}"
    attach = _secrets.token_urlsafe(32)
    rec = AttachRecord(
        user_sub=user_sub,
        sandbox_name=sb.name,
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
    tok = _secrets.token_urlsafe(32)
    _attach_store[tok] = rec
    return tok


def sandbox_for_user(user_sub: str) -> Optional[Sandbox]:
    """Return the claimed sandbox for a user, or None if not allocated."""
    name = _user_to_sandbox.get(user_sub)
    if not name:
        return None
    return _claimed.get(name)


async def destroy_sandbox(user_sub: str) -> None:
    name = _user_to_sandbox.pop(user_sub, None)
    if not name:
        return
    sb = _claimed.pop(name, None)
    if sb:
        sb.state = "Terminating"
    await _delete_aci(name)
    logger.info("destroyed sandbox %s for user %s", name, user_sub)
