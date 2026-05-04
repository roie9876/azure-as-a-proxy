"""Configuration loaded from env vars (set by the ACA Container App)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Logging
    broker_log_level: str = Field("INFO")

    # Azure (for ACI provisioning)
    azure_subscription_id: str = Field("")
    azure_resource_group: str = Field("")
    azure_location: str = Field("swedencentral")

    # Sandbox image (kiosk Chromium) deployed as ACI per session
    sandbox_image: str = Field("")
    sandbox_subnet_id: str = Field("", description="VNet subnet (delegated to Microsoft.ContainerInstance/containerGroups)")

    # SaaS URL the sandbox Chromium opens in --kiosk --app=<URL>.
    # README §4 step 6: sandbox is a fresh Chromium pointed at the SaaS.
    saas_url: str = Field("https://example.com", description="Target SaaS URL pinned per sandbox")

    # ACR creds for sandbox image pull (passed to ACI imageRegistryCredentials)
    acr_name: str = Field("")
    acr_server: str = Field("")
    acr_username: str = Field("")
    acr_password: str = Field("")

    # Warm pool: keep N idle sandboxes ready for instant alloc
    warm_pool_size: int = Field(2, ge=0, le=20)

    # VNC password for Kasm sandbox (broker proxies, user never types it).
    # PoC: single shared value injected into every ACI as VNC_PW.
    sandbox_vnc_password: str = Field("cloak-poc-vnc")

    # Sandbox serves noVNC + websockify on the same HTTP port.
    # accetto/ubuntu-vnc-xfce-chromium-g3 → http://:6901
    sandbox_port: int = Field(6901)
    sandbox_scheme: str = Field("http")

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
