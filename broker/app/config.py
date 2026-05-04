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
    # The SaaS itself authenticates the human inside the ACI;
    # the broker does NOT authenticate users.
    saas_url: str = Field("https://example.com", description="Target SaaS URL pinned per sandbox")

    # ACR creds for sandbox image pull (passed to ACI imageRegistryCredentials)
    acr_name: str = Field("")
    acr_server: str = Field("")
    acr_username: str = Field("")
    acr_password: str = Field("")

    # Warm pool: keep N idle sandboxes ready for instant alloc
    warm_pool_size: int = Field(2, ge=0, le=20)

    # VNC password for sandbox (broker proxies, user never types it).
    sandbox_vnc_password: str = Field("cloak-poc-vnc")

    # Sandbox serves noVNC + websockify on the same HTTP port.
    sandbox_port: int = Field(6901)
    sandbox_scheme: str = Field("http")

    # Session lifecycle
    attach_token_ttl_seconds: int = Field(60)
    session_idle_timeout_seconds: int = Field(900)
    browser_id_ttl_seconds: int = Field(60 * 60 * 8)  # 8h


settings = Settings()
