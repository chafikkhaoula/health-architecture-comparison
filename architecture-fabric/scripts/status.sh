#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

compose ps

printf '\n=== ORDERER CHANNEL ===\n'
"$OSNADMIN" channel list \
  --channelID "$CHANNEL_NAME" \
  --orderer-address "127.0.0.1:$ORDERER_ADMIN_PORT" \
  --ca-file "$ORDERER_TLS_CA" \
  --client-cert "$ORDERER_TLS_CERT" \
  --client-key "$ORDERER_TLS_KEY"

printf '\n=== COMMITTED CHAINCODE ===\n'
use_org1
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode querycommitted \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --output json

printf '\n=== GATEWAY ===\n'
curl --fail --silent --show-error \
  "http://127.0.0.1:$GATEWAY_BRIDGE_PORT/healthz"
printf '\n'
