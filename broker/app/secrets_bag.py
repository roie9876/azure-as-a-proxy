"""Secrets bootstrap from Key Vault using managed identity."""
from __future__ import annotations

import logging
import secrets as _secrets

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from .config import settings

logger = logging.getLogger(__name__)


class SecretBag:
    """Lazy holder for runtime secrets. Falls back to a random in-memory secret
    in PoC / local dev when Key Vault is unreachable."""

    session_secret: str
    oidc_client_secret: str | None

    def __init__(self) -> None:
        self.session_secret = _secrets.token_urlsafe(48)  # PoC fallback
        self.oidc_client_secret = None

    def load(self) -> None:
        if not settings.key_vault_name:
            logger.warning("KEY_VAULT_NAME not set; using ephemeral in-memory secrets (PoC mode).")
            return
        try:
            cred = DefaultAzureCredential()
            client = SecretClient(
                vault_url=f"https://{settings.key_vault_name}.vault.azure.net",
                credential=cred,
            )
            try:
                self.session_secret = client.get_secret(settings.session_secret_name).value or self.session_secret
            except Exception as ex:  # noqa: BLE001
                logger.warning("Could not load session secret from KV (%s); using ephemeral.", ex)
            if not settings.stub_auth:
                try:
                    self.oidc_client_secret = client.get_secret(settings.oidc_client_secret_name).value
                except Exception as ex:  # noqa: BLE001
                    logger.error("OIDC client secret missing in KV: %s", ex)
        except Exception as ex:  # noqa: BLE001
            logger.warning("Key Vault unreachable (%s); continuing with ephemeral secrets.", ex)


bag = SecretBag()
