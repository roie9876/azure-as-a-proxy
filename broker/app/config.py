"""Configuration loaded from env vars (set by the ACA Container App)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Logging
    broker_log_level: str = Field("INFO")

    # ACA Dynamic Sessions
    session_pool_endpoint: str = Field("", description="poolManagementEndpoint URL")
    session_pool_resource_id: str = Field("")

    # Key Vault (managed identity is used for auth)
    key_vault_name: str = Field("")

    # External OIDC IdP (Auth0 / Okta / Keycloak / Entra)
    oidc_issuer: str = Field("", description="OIDC issuer URL; empty = stub auth (PoC only)")
    oidc_client_id: str = Field("")
    oidc_client_secret_name: str = Field("oidc-client-secret", description="Key Vault secret name")
    oidc_redirect_path: str = Field("/auth/callback")

    # Allowlist of users (sub claim, email, or upn). Comma-separated.
    user_allowlist: str = Field("")

    # Session lifecycle
    attach_token_ttl_seconds: int = Field(60)
    session_idle_timeout_seconds: int = Field(900)

    # Cookie/session secret name in Key Vault (for state cookie & attach-token signing).
    session_secret_name: str = Field("broker-session-secret")

    @property
    def stub_auth(self) -> bool:
        return not self.oidc_issuer or not self.oidc_client_id

    @property
    def allowlist_set(self) -> set[str]:
        return {x.strip().lower() for x in self.user_allowlist.split(",") if x.strip()}


settings = Settings()
