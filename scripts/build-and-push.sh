#!/usr/bin/env bash
# Build broker + sandbox images, push to ACR.
# Usage: ./scripts/build-and-push.sh <acrName>
set -euo pipefail

ACR="${1:?usage: $0 <acrName>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[+] az acr login -n $ACR"
az acr login -n "$ACR"

LOGIN_SERVER="$(az acr show -n "$ACR" --query loginServer -o tsv)"

echo "[+] Build broker"
docker buildx build --platform linux/amd64 \
  -t "$LOGIN_SERVER/cloak-broker:latest" \
  -f "$ROOT/broker/Dockerfile" \
  --push \
  "$ROOT/broker"

echo "[+] Build sandbox"
docker buildx build --platform linux/amd64 \
  -t "$LOGIN_SERVER/cloak-sandbox:latest" \
  -f "$ROOT/sandbox/Dockerfile" \
  --push \
  "$ROOT/sandbox"

echo "[+] Done."
echo "    Broker  : $LOGIN_SERVER/cloak-broker:latest"
echo "    Sandbox : $LOGIN_SERVER/cloak-sandbox:latest"
