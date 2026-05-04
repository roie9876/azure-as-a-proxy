#!/usr/bin/env bash
# Cloak entrypoint — wraps the upstream Kasm chromium launcher with fingerprint
# normalization flags, then execs the upstream entrypoint.
set -euo pipefail

# Defaults if not provided by ACA.
: "${CHROME_USER_AGENT:=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36}"
: "${CHROME_ACCEPT_LANG:=en-US,en;q=0.9}"
: "${LANG:=en_US.UTF-8}"
: "${TZ:=Europe/Stockholm}"

export LANG TZ

# Kasm reads CHROME_ARGS for additional Chromium flags.
EXTRA_ARGS=(
  "--user-agent=${CHROME_USER_AGENT}"
  "--accept-lang=${CHROME_ACCEPT_LANG}"
  "--lang=en-US"
  "--disable-features=AudioServiceOutOfProcess,UserAgentClientHint"
  "--disable-webrtc-hw-encoding"
  "--no-default-browser-check"
  "--no-first-run"
)

# Append to whatever the upstream image already sets.
if [[ -n "${CHROME_ARGS:-}" ]]; then
  export CHROME_ARGS="${CHROME_ARGS} ${EXTRA_ARGS[*]}"
else
  export CHROME_ARGS="${EXTRA_ARGS[*]}"
fi

# Hand off to upstream Kasm entrypoint.
exec /dockerstartup/kasm_default_profile.sh "$@"
