# syntax=docker/dockerfile:1.7
# Cloak sandbox — kiosk Chromium streamed via VNC/websockify.
#
# Per README §4 / §8.1: user sees ONLY the SaaS rendered inside the sandbox's
# Chromium, streamed as pixels. No desktop, no taskbar, no file manager,
# no DevTools, no extensions, no clipboard.
#
# Stack inside the container:
#   Xvfb (:99)  ->  fluxbox (minimal WM, just to host a fullscreen window)
#                ->  x11vnc -display :99 -forever -shared
#                ->  websockify 6901 -> localhost:5900 (raw VNC)
#                ->  chromium --kiosk --app=$SAAS_URL
#
# Ports:
#   6901 — websockify (broker proxies WS frames here)
#   6902 — file-inbox HTTP (broker forwards user uploads here)
FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    SCREEN_GEOMETRY=1920x1080x24 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TZ=Europe/Stockholm \
    SAAS_URL=https://example.com

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates locales tzdata \
        xvfb x11vnc fluxbox \
        python3 python3-numpy websockify novnc \
        chromium chromium-sandbox \
        fonts-liberation fonts-noto-color-emoji \
        dbus-x11 \
        procps \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# Chromium managed-policy lockdown (no DevTools, no extensions, no clipboard,
# no downloads, no print, no incognito, no popups, no URL bar via kiosk).
RUN mkdir -p /etc/chromium/policies/managed
COPY chromium-policies.json /etc/chromium/policies/managed/cloak.json

# Non-root user
RUN useradd -m -u 1000 -s /bin/bash cloak

COPY entrypoint.sh /usr/local/bin/cloak-entrypoint.sh
COPY file-inbox.py /usr/local/bin/cloak-file-inbox.py
RUN chmod +x /usr/local/bin/cloak-entrypoint.sh /usr/local/bin/cloak-file-inbox.py

EXPOSE 6901 6902
USER cloak
WORKDIR /home/cloak

ENTRYPOINT ["/usr/local/bin/cloak-entrypoint.sh"]
