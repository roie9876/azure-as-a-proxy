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
# Device emulation. The broker sets DEVICE_PROFILE=mobile (plus a portrait
# SCREEN_GEOMETRY, a mobile CHROME_USER_AGENT and a DEVICE_SCALE_FACTOR) when
# the *real* client browser is a phone, so the SaaS renders its mobile layout.
: "${DEVICE_PROFILE:=desktop}"
: "${DEVICE_SCALE_FACTOR:=1}"

# Derive the Chromium window size from the Xvfb geometry (WxHxDepth) so the
# kiosk window always fills the virtual screen, portrait or landscape.
WIN_W="${SCREEN_GEOMETRY%%x*}"
_geom_rest="${SCREEN_GEOMETRY#*x}"
WIN_H="${_geom_rest%%x*}"

echo "[cloak] SAAS_URL=${SAAS_URL}"
echo "[cloak] DISPLAY=${DISPLAY} SCREEN=${SCREEN_GEOMETRY} PROFILE=${DEVICE_PROFILE} DSF=${DEVICE_SCALE_FACTOR}"

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

# 4b. file-inbox — accept POST /inbox uploads from the broker, drop into
# ~/uploads/ so Chromium's <input type=file> picker can attach them.
# Bound on 0.0.0.0:6902 because the broker reaches us via VNet IP; subnet NSG
# blocks every other source. Optional shared-secret check via INBOX_TOKEN.
mkdir -p "${HOME}/uploads"
chmod 700 "${HOME}/uploads"

# Defense-in-depth: even though the Cloak picker extension hijacks the GTK
# file dialog so the user never sees the desktop, scrub the home dir of
# anything that would be embarrassing if the extension ever fails-open.
# The kiosk runs as user `cloak` with no document templates, so this is
# mostly empty already, but we delete the standard XDG dirs to be sure.
for d in Desktop Documents Downloads Music Pictures Public Templates Videos; do
  rm -rf "${HOME}/${d}" 2>/dev/null || true
done
chmod 700 "${HOME}"
# GTK bookmarks: only show ~/uploads in the file dialog sidebar (in case
# the extension is ever bypassed; the dialog at least won't list user dirs).
mkdir -p "${HOME}/.config/gtk-3.0"
printf 'file://%s/uploads Sandbox uploads\n' "${HOME}" > "${HOME}/.config/gtk-3.0/bookmarks"

INBOX_PORT=6902 INBOX_DIR="${HOME}/uploads" \
  python3 /usr/local/bin/cloak-file-inbox.py >/tmp/file-inbox.log 2>&1 &
INBOX_PID=$!
sleep 0.2

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

# Mobile emulation flags. A narrow portrait window already makes responsive
# (CSS media-query) sites switch layout; these flags also flip pointer/hover
# media features to "touch" and apply a device-scale-factor so UA- and
# pointer-sniffing SaaS render their proper mobile site at legible size.
MOBILE_FLAGS=()
if [[ "${DEVICE_PROFILE}" == "mobile" ]]; then
  MOBILE_FLAGS+=(
    --touch-events=enabled
    --force-device-scale-factor="${DEVICE_SCALE_FACTOR}"
    --blink-settings=primaryHoverType=4,availableHoverTypes=4,primaryPointerType=2,availablePointerTypes=2
  )
  echo "[cloak] DEVICE_PROFILE=mobile — touch + dsf=${DEVICE_SCALE_FACTOR} emulation on"
fi

CHROME_FLAGS=(
  --kiosk
  --app="${SAAS_URL}"
  "${INSECURE_FLAGS[@]}"
  "${MOBILE_FLAGS[@]}"
  --no-first-run
  --no-default-browser-check
  --noerrdialogs
  --disable-translate
  --disable-features=TranslateUI,DownloadBubble
  --disable-pinch
  --disable-component-update
  --disable-background-networking
  --disable-sync
  # Load ONLY the Cloak picker extension; block any other extension load
  # path (Web Store, drag-drop, dev mode). The picker hijacks <input
  # type=file> clicks and shows a sandbox-only file list, so the SaaS
  # never triggers the GTK Open File dialog (which would expose the
  # desktop in the streamed view).
  --load-extension=/opt/cloak-picker
  --disable-extensions-except=/opt/cloak-picker
  --disable-dev-shm-usage
  --user-agent="${CHROME_USER_AGENT}"
  --accept-lang="${CHROME_ACCEPT_LANG}"
  --lang=en-US
  --window-position=0,0
  --window-size=${WIN_W},${WIN_H}
  --start-fullscreen
  --user-data-dir=/tmp/chrome-profile
)

# Trap to clean up children on exit
cleanup() {
  echo "[cloak] shutting down"
  kill "${INBOX_PID}" "${WS_PID}" "${X11VNC_PID}" "${FLUX_PID}" "${XVFB_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[cloak] launching Chromium kiosk -> ${SAAS_URL}"
# exec so chromium becomes PID-foreground; container exits when chromium dies.
exec chromium "${CHROME_FLAGS[@]}"
