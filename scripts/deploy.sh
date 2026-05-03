#!/usr/bin/env bash
# Deploy infra via Bicep at subscription scope.
# Usage:
#   ./scripts/deploy.sh [<brokerImage>] [<sandboxImage>]
# Env:
#   AZURE_SUBSCRIPTION_ID  (required)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUB="${AZURE_SUBSCRIPTION_ID:?set AZURE_SUBSCRIPTION_ID}"
LOC="${LOCATION:-swedencentral}"
PARAM_FILE="$ROOT/infra/main.bicepparam"

EXTRA=()
if [[ -n "${1:-}" ]]; then EXTRA+=("--parameters" "brokerImage=$1"); fi
if [[ -n "${2:-}" ]]; then EXTRA+=("--parameters" "sandboxImage=$2"); fi

az account set --subscription "$SUB"

echo "[+] Validating Bicep..."
az deployment sub validate \
  --location "$LOC" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "$PARAM_FILE" \
  "${EXTRA[@]}"

echo "[+] Deploying..."
az deployment sub create \
  --name "cloak-$(date +%Y%m%d-%H%M%S)" \
  --location "$LOC" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "$PARAM_FILE" \
  "${EXTRA[@]}" \
  --query 'properties.outputs' -o json
