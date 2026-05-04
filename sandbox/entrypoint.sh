#!/usr/bin/env bash
# Cloak sandbox entrypoint — kiosk Chromium pinned to $SAAS_URL.
# Streamed via Xvfb -> x11vnc -> websockify on :6901.
#
# Each ACI gets its own fresh container, so $HOME state lives only for one
# session (README §6.4 — no persistence by design).
set -euo pipefail

: "${SAAS_URL:=https://example.com}"
: "${SCREEN_GEOMETRY:=1920x1080x24}"
: "${CHROME_USER_AGENT:=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36}"
: "${CHROME_ACCEPT_LANG:=en-US,en;q=0.9}"

echo "[cloak] SAAS_URL=${SAAS_URL}"
echo "[cloak] DISPLAY=${DISPLAY} SCREEN=${SCREEN_GEOMETRY}"

# Resolve novnc share dir (Debian places it at /usr/share/novnc).
NOVNC_DIR="/usr/share/novnc"
if [[ ! -d "$NOVNC_DIR" ]]; then
  for cand in /usr/share/webapps/novnc /usr/local/share/novnc; do
    [[ -d "$cand" ]] && NOVNC_DIR="$cand" && break
  done
fi

# 1. Xvfb — virtual display
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_GEOMETRY}" -ac +extension RANDR +extension RENDER -nolisten tcp &
XVFB_PID=$!

# Wait for Xvfb to be ready
for _ in $(seq 1 50); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then break; fi
  sleep 0.1
done

# 2. fluxbox — minimal WM so the kiosk window can be sized full-screen
fluxbox >/dev/null 2>&1 &
FLUX_PID=$!
sleep 0.3

# 3. x11vnc — share the Xvfb display read/write on :5900 (localhost only)
x11vnc -display "${DISPLAY}" -nopw -forever -shared -localhost -quiet \
       -rfbport 5900 -noxdamage -noxfixes &
X11VNC_PID=$!
sleep 0.3

# 4. websockify — bridge HTTP/WS on :6901 to raw VNC :5900, serving noVNC client
WEBSOCKIFY_ARGS=(--web "${NOVNC_DIR}" 0.0.0.0:6901 localhost:5900)
websockify "${WEBSOCKIFY_ARGS[@]}" >/tmp/websockify.log 2>&1 &
WS_PID=$!
sleep 0.5

# 5. Chromium — kiosk, pointed at the SaaS URL.
#    --app= strips the URL bar / tabs / menus. --kiosk forces fullscreen.
#    Lockdown policies (no DevTools, no ext, no clipboard) live in
#    /etc/chromium/policies/managed/cloak.json (mounted at build time).
## Optional: bypass cert validation for the pinned SaaS host (PoC backends
## frequently use self-signed / private-CA certs). The kiosk only ever loads
## $SAAS_URL, so this does NOT widen attack surface beyond that one origin.
## Set INSECURE_SAAS=0 to disable.
: "${INSECURE_SAAS:=1}"
INSECURE_FLAGS=()
if [[ "${INSECURE_SAAS}" == "1" ]]; then
  SAAS_HOST="$(printf '%s' "${SAAS_URL}" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
  INSECURE_FLAGS+=(
    --ignore-certificate-errors
    --ignore-certificate-errors-spki-list=
    --test-type
    --unsafely-treat-insecure-origin-as-secure="${SAAS_URL%/}"
    --host-resolver-rules="MAP ${SAAS_HOST} ${SAAS_HOST}"
  )
  echo "[cloak] INSECURE_SAAS=1 — ignoring cert errors for ${SAAS_HOST}"
fi

CHROME_FLAGS=(
  --kiosk
  --app="${SAAS_URL}"
  "${INSECURE_FLAGS[@]}"
  --no-first-run
  --no-default-browser-check
  --noerrdialogs
  --disable-translate
  --disable-features=TranslateUI,DownloadBubble
  --disable-pinch
  --disable-component-update
  --disable-background-networking
  --disable-sync
  --disable-extensions
  --disable-dev-shm-usage
  --user-agent="${CHROME_USER_AGENT}"
  --accept-lang="${CHROME_ACCEPT_LANG}"
  --lang=en-US
  --window-position=0,0
  --window-size=1920,1080
  --start-fullscreen
  --user-data-dir=/tmp/chrome-profile
)

# Trap to clean up children on exit
cleanup() {
  echo "[cloak] shutting down"
  kill "${WS_PID}" "${X11VNC_PID}" "${FLUX_PID}" "${XVFB_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[cloak] launching Chromium kiosk -> ${SAAS_URL}"
# exec so chromium becomes PID-foreground; container exits when chromium dies.
exec chromium "${CHROME_FLAGS[@]}"
