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
    # Idle mobile-profile sandboxes kept warm so phone sessions don't cold-start
    # (a cold ACI start exceeds the Front Door origin-response timeout -> 504).
    mobile_warm_pool_size: int = Field(1, ge=0, le=10)

    # VNC password for sandbox (broker proxies, user never types it).
    sandbox_vnc_password: str = Field("cloak-poc-vnc")

    # Sandbox serves noVNC + websockify on the same HTTP port.
    sandbox_port: int = Field(6901)
    sandbox_scheme: str = Field("http")

    # Device emulation. The broker reads the *real* client's User-Agent /
    # Sec-CH-UA-Mobile headers (the phone talks to the broker directly) and
    # provisions the sandbox Chromium in the matching profile, so the SaaS
    # renders its desktop or mobile layout. noVNC `resize=remote` then fits
    # the (portrait) framebuffer to the phone screen.
    mobile_emulation_enabled: bool = Field(True)
    desktop_screen_geometry: str = Field("1920x1080x24")
    desktop_user_agent: str = Field(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    mobile_screen_geometry: str = Field("390x844x24", description="Portrait WxHxDepth for phone sandboxes; this IS the CSS viewport width — keep <768 so SaaS serves its mobile layout (390x844 = iPhone 12-15 logical)")
    mobile_device_scale_factor: float = Field(1.0, description="Chromium --force-device-scale-factor for phone sandboxes; kept at 1 because on Xvfb/kiosk a higher DSF does NOT shrink the CSS layout viewport (it only over-sizes the window), so it cannot be used to force a mobile width")
    mobile_user_agent: str = Field(
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36"
    )

    # Session lifecycle
    attach_token_ttl_seconds: int = Field(60)
    session_idle_timeout_seconds: int = Field(900)
    browser_id_ttl_seconds: int = Field(60 * 60 * 8)  # 8h

    # File upload (broker-mediated). User picks file in their own browser, broker
    # forwards bytes to the claimed sandbox's file-inbox so Chromium's file
    # picker can attach them. See docs/UPLOAD.md.
    upload_enabled: bool = Field(True)
    upload_max_bytes: int = Field(100 * 1024 * 1024)        # per file: 100 MB
    upload_session_max_bytes: int = Field(500 * 1024 * 1024) # per browser: 500 MB
    upload_mime_allowlist: str = Field(
        "application/pdf,"
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
        "application/vnd.openxmlformats-officedocument.presentationml.presentation,"
        "application/msword,application/vnd.ms-excel,application/vnd.ms-powerpoint,"
        "image/png,image/jpeg,image/gif,image/webp,"
        "text/plain,text/csv,text/markdown,"
        "application/zip,application/json,application/xml",
        description="Comma-separated allowlist of Content-Type values accepted by /upload",
    )
    sandbox_inbox_port: int = Field(6902)
    sandbox_inbox_token: str = Field("", description="Optional shared secret broker<->sandbox inbox")


settings = Settings()
