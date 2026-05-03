#!/usr/bin/env bash
# Smoke-test: hit the Front Door endpoint and assert that identifying response
# headers are NOT present (Surface 1 cloaking check).
# Usage: ./scripts/smoke-test.sh <frontDoorHostname>
set -euo pipefail

HOST="${1:?usage: $0 <frontDoorHostname>}"

echo "[+] HEAD https://$HOST/"
HEADERS="$(curl -sS -I "https://$HOST/" || true)"
echo "$HEADERS"
echo

FAIL=0
check_absent() {
  local h="$1"
  if echo "$HEADERS" | grep -i "^$h:" >/dev/null; then
    echo "  [FAIL] $h is present (should be stripped)"
    FAIL=1
  else
    echo "  [ OK ] $h absent"
  fi
}

echo "[+] Verifying SaaS-identity headers are stripped:"
# Note: Via, X-Azure-Ref, X-Azure-FDID, X-MSEdge-Ref, X-Cache are FD-reserved
# (rules engine refuses to modify them). They will appear but only reveal
# 'behind some Azure CDN' — no tenant or SaaS identity. Documented residual signals.
check_absent "Server"
check_absent "X-Powered-By"
check_absent "X-Request-Id"
check_absent "X-Correlation-Id"
check_absent "X-Nextjs-Cache"
check_absent "X-Nextjs-Prerender"
check_absent "X-Nextjs-Stale-Time"

echo
echo "[+] Verifying privacy headers are present:"
check_present() {
  local h="$1"
  if echo "$HEADERS" | grep -i "^$h:" >/dev/null; then
    echo "  [ OK ] $h present"
  else
    echo "  [FAIL] $h missing"
    FAIL=1
  fi
}
check_present "Referrer-Policy"
check_present "X-Content-Type-Options"
check_present "X-Frame-Options"
check_present "Permissions-Policy"

if [[ "$FAIL" -ne 0 ]]; then
  echo
  echo "[!] Smoke test FAILED."
  exit 1
fi
echo
echo "[+] Smoke test PASSED."
