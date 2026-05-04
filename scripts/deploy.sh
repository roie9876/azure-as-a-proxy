#!/usr/bin/env bash
# One-shot deploy: provisions everything, auto-approves the Front Door
# shared private-endpoint, seeds the broker session secret in Key Vault,
# restarts the broker so it picks up the secret, and waits for /healthz=200.
#
# Usage:
#   export AZURE_SUBSCRIPTION_ID=<sub-id>
#   az login
#   ./scripts/deploy.sh                                  # uses GHCR public images
#   ./scripts/deploy.sh <brokerImage> <sandboxImage>     # override images
#
# Env:
#   AZURE_SUBSCRIPTION_ID  (required)
#   LOCATION               (default: swedencentral)
#   NAME_PREFIX            (default: cloak — must match infra/main.bicepparam)
#   SKIP_HEALTH_WAIT       (default: 0; set to 1 to skip the /healthz poll)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUB="${AZURE_SUBSCRIPTION_ID:?set AZURE_SUBSCRIPTION_ID}"
LOC="${LOCATION:-swedencentral}"
PREFIX="${NAME_PREFIX:-cloak}"
RG="rg-${PREFIX}-${LOC}"
ACA_ENV="cae-${PREFIX}"
BROKER_APP="ca-${PREFIX}-broker"
PARAM_FILE="$ROOT/infra/main.bicepparam"

EXTRA=()
if [[ -n "${1:-}" ]]; then EXTRA+=("--parameters" "brokerImage=$1"); fi
if [[ -n "${2:-}" ]]; then EXTRA+=("--parameters" "sandboxImage=$2"); fi

echo "[1/6] az account set --subscription $SUB"
az account set --subscription "$SUB"

echo "[2/6] Validating Bicep..."
az deployment sub validate \
  --location "$LOC" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "$PARAM_FILE" \
  ${EXTRA[@]+"${EXTRA[@]}"} >/dev/null

echo "[3/6] Deploying (8-15 min on a fresh subscription)..."
DEP_NAME="cloak-$(date +%Y%m%d-%H%M%S)"
OUTPUTS=$(az deployment sub create \
  --name "$DEP_NAME" \
  --location "$LOC" \
  --template-file "$ROOT/infra/main.bicep" \
  --parameters "$PARAM_FILE" \
  ${EXTRA[@]+"${EXTRA[@]}"} \
  --query 'properties.outputs' -o json)

FD_EP=$(echo "$OUTPUTS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['frontDoorEndpoint']['value'])")
BROKER_FQDN=$(echo "$OUTPUTS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['brokerFqdn']['value'])")
NAT_IP=$(echo "$OUTPUTS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['natGatewayPublicIp']['value'])")

echo "    frontDoorEndpoint = $FD_EP"
echo "    brokerFqdn        = $BROKER_FQDN"
echo "    natGatewayPublicIp= $NAT_IP"

echo "[4/6] Approving Front Door shared Private Link to ACA env (if Pending)..."
PENDING=$(az network private-endpoint-connection list \
  --resource-group "$RG" --name "$ACA_ENV" \
  --type Microsoft.App/managedEnvironments \
  --query "[?properties.privateLinkServiceConnectionState.status=='Pending'].name" -o tsv 2>/dev/null || true)
if [[ -n "$PENDING" ]]; then
  for n in $PENDING; do
    echo "    approving $n"
    az network private-endpoint-connection approve \
      --resource-group "$RG" --name "$n" --resource-name "$ACA_ENV" \
      --type Microsoft.App/managedEnvironments \
      --description "Approved by deploy.sh" >/dev/null
  done
else
  echo "    (no Pending connections)"
fi

echo "[5/6] Seeding broker-session-secret in Key Vault (idempotent)..."
KV_NAME=$(az keyvault list -g "$RG" --query "[?starts_with(name,'kv-${PREFIX}')].name | [0]" -o tsv 2>/dev/null || true)
if [[ -n "$KV_NAME" && "$KV_NAME" != "null" ]]; then
  if ! az keyvault secret show --vault-name "$KV_NAME" --name broker-session-secret >/dev/null 2>&1; then
    echo "    creating broker-session-secret in $KV_NAME"
    ME_OID=$(az ad signed-in-user show --query id -o tsv)
    KV_ID=$(az keyvault show -g "$RG" -n "$KV_NAME" --query id -o tsv)
    az role assignment create \
      --assignee-object-id "$ME_OID" --assignee-principal-type User \
      --role "Key Vault Secrets Officer" --scope "$KV_ID" >/dev/null 2>&1 || true
    sleep 20  # RBAC propagation
    SECRET_VAL=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
    az keyvault secret set --vault-name "$KV_NAME" --name broker-session-secret \
      --value "$SECRET_VAL" >/dev/null
    echo "    forcing broker revision restart"
    az containerapp update -g "$RG" -n "$BROKER_APP" \
      --revision-suffix "secret-$(date +%H%M%S)" >/dev/null
  else
    echo "    (broker-session-secret already exists)"
  fi
else
  echo "    (no Key Vault found with prefix kv-${PREFIX} — skipping secret seed)"
fi

if [[ "${SKIP_HEALTH_WAIT:-0}" == "1" ]]; then
  echo "[6/6] SKIP_HEALTH_WAIT=1 — not polling /healthz"
else
  echo "[6/6] Polling https://${FD_EP}/healthz (up to 12 min for FD propagation)..."
  ok=0
  for i in $(seq 1 72); do
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 "https://${FD_EP}/healthz" || true)
    if [[ "$code" == "200" ]]; then
      echo "    [$i] HTTP 200 — broker is reachable"
      ok=1
      break
    fi
    printf "    [%2d] HTTP %s — waiting 10s\n" "$i" "$code"
    sleep 10
  done
  if [[ "$ok" != "1" ]]; then
    echo "    WARN: /healthz did not return 200 within 12 min."
    echo "    Check: az containerapp logs show -g $RG -n $BROKER_APP --tail 50"
    exit 1
  fi
fi

cat <<EOF

=========================================================================
  Deployment complete.
  Open in your browser:
      https://${FD_EP}/
  Broker FQDN (internal):  ${BROKER_FQDN}
  NAT Gateway egress IP:   ${NAT_IP}
=========================================================================
EOF
