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
  ${EXTRA[@]+"${EXTRA[@]}"} >/dev/null

echo "[+] Deploying..."
OUTPUTS=$(az deployment sub create \
  --name "cloak-$(date +%Y%m%d-%H%M%S)" \
  --location "$LOC" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "$PARAM_FILE" \
  ${EXTRA[@]+"${EXTRA[@]}"} \
  --query 'properties.outputs' -o json)

echo "$OUTPUTS"

KV_NAME=$(echo "$OUTPUTS" | python3 -c "import json,sys;print(json.load(sys.stdin)['keyVaultName']['value'])")
SECRET_NAME="broker-session-secret"

echo "[+] Ensuring $SECRET_NAME exists in Key Vault $KV_NAME..."
if az keyvault secret show --vault-name "$KV_NAME" --name "$SECRET_NAME" --query id -o tsv >/dev/null 2>&1; then
  echo "    secret already exists — leaving as-is."
else
  ME=$(az ad signed-in-user show --query id -o tsv)
  KV_ID=$(az keyvault show -n "$KV_NAME" --query id -o tsv)
  # Best-effort RBAC grant; ignore failure if caller already has it.
  az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
    --role "Key Vault Secrets Officer" --scope "$KV_ID" >/dev/null 2>&1 || true
  echo "    waiting 30s for RBAC propagation..."
  sleep 30
  VAL=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
  az keyvault secret set --vault-name "$KV_NAME" --name "$SECRET_NAME" --value "$VAL" --output none
  echo "    secret created."
fi

echo "[+] Done."
