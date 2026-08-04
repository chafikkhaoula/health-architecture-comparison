#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_command docker
require_command go
require_command python3
require_command curl
for binary in "$CRYPTOGEN" "$CONFIGTXGEN" "$OSNADMIN" "$PEER"; do
  require_executable "$binary"
done

if ! "$CRYPTOGEN" version 2>&1 |
  grep -Fq "Version: v$FABRIC_VERSION"; then
  echo "cryptogen does not match Fabric v$FABRIC_VERSION" >&2
  exit 1
fi
if ! "$CONFIGTXGEN" -version 2>&1 |
  grep -Fq "Version: v$FABRIC_VERSION"; then
  echo "configtxgen does not match Fabric v$FABRIC_VERSION" >&2
  exit 1
fi
if ! "$PEER" version 2>&1 |
  grep -Fq "Version: v$FABRIC_VERSION"; then
  echo "peer does not match Fabric v$FABRIC_VERSION" >&2
  exit 1
fi

if [ -e "$ORGANIZATIONS_DIR" ] || [ -e "$ARTIFACTS_DIR" ]; then
  echo "Generated Fabric state already exists." >&2
  echo "Run scripts/reset.sh before a clean deployment." >&2
  exit 1
fi

mkdir -p "$ARTIFACTS_DIR"

echo "Generating two-organization cryptographic material"
"$CRYPTOGEN" generate \
  --config="$NETWORK_DIR/crypto-config.yaml" \
  --output="$ORGANIZATIONS_DIR"

echo "Generating the application-channel genesis block"
(
  cd "$NETWORK_DIR"
  FABRIC_CFG_PATH="$NETWORK_DIR" "$CONFIGTXGEN" \
    -profile HealthChannel \
    -channelID "$CHANNEL_NAME" \
    -outputBlock "$ARTIFACTS_DIR/$CHANNEL_NAME.block"
)

echo "Starting isolated Fabric nodes and off-chain PostgreSQL"
compose up -d orderer peer0org1 peer0org2 postgres

echo "Waiting for orderer and peers"
for attempt in $(seq 1 60); do
  orderer_ready=false
  if "$OSNADMIN" channel list \
    --orderer-address "127.0.0.1:$ORDERER_ADMIN_PORT" \
    --ca-file "$ORDERER_TLS_CA" \
    --client-cert "$ORDERER_TLS_CERT" \
    --client-key "$ORDERER_TLS_KEY" \
    >/dev/null 2>&1; then
    orderer_ready=true
  fi
  use_org1
  org1_ready=false
  # Exercise the peer channel/endorser path used by the next operation.
  # `peer node status` can succeed before channel services finish starting.
  if FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" channel list \
    >/dev/null 2>&1; then
    org1_ready=true
  fi
  use_org2
  org2_ready=false
  if FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" channel list \
    >/dev/null 2>&1; then
    org2_ready=true
  fi
  if [ "$orderer_ready" = true ] && \
    [ "$org1_ready" = true ] && \
    [ "$org2_ready" = true ]; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    compose logs --no-color orderer peer0org1 peer0org2
    echo "Fabric orderer and peers did not become ready." >&2
    exit 1
  fi
  sleep 2
done

echo "Joining the orderer to $CHANNEL_NAME"
"$OSNADMIN" channel join \
  --channelID "$CHANNEL_NAME" \
  --config-block "$ARTIFACTS_DIR/$CHANNEL_NAME.block" \
  --orderer-address "127.0.0.1:$ORDERER_ADMIN_PORT" \
  --ca-file "$ORDERER_TLS_CA" \
  --client-cert "$ORDERER_TLS_CERT" \
  --client-key "$ORDERER_TLS_KEY"

echo "Joining Org1 and Org2 peers to $CHANNEL_NAME"
use_org1
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" channel join \
  --blockpath "$ARTIFACTS_DIR/$CHANNEL_NAME.block"
use_org2
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" channel join \
  --blockpath "$ARTIFACTS_DIR/$CHANNEL_NAME.block"

echo "Preparing deterministic Go chaincode package"
(
  cd "$CHAINCODE_DIR"
  go mod download
  go mod vendor
)
CHAINCODE_PACKAGE="$ARTIFACTS_DIR/${CHAINCODE_NAME}.tar.gz"
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" lifecycle chaincode package \
  "$CHAINCODE_PACKAGE" \
  --path "$CHAINCODE_DIR" \
  --lang golang \
  --label "${CHAINCODE_NAME}_${CHAINCODE_VERSION}"

echo "Installing chaincode on both peers"
use_org1
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" lifecycle chaincode install \
  "$CHAINCODE_PACKAGE"
use_org2
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" lifecycle chaincode install \
  "$CHAINCODE_PACKAGE"

use_org1
PACKAGE_ID="$(
  FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
    lifecycle chaincode queryinstalled --output json |
    python3 -c '
import json
import os
import sys

label = os.environ["CHAINCODE_NAME"] + "_" + os.environ["CHAINCODE_VERSION"]
data = json.load(sys.stdin)
matches = [
    item["package_id"]
    for item in data.get("installed_chaincodes", [])
    if item.get("label") == label
]
if len(matches) != 1:
    raise SystemExit(f"expected one installed package for {label}, got {len(matches)}")
print(matches[0])
'
)"

POLICY="AND('Org1MSP.peer','Org2MSP.peer')"

echo "Approving the chaincode definition for Org1"
use_org1
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode approveformyorg \
  "${orderer_flags[@]}" \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --version "$CHAINCODE_VERSION" \
  --package-id "$PACKAGE_ID" \
  --sequence "$CHAINCODE_SEQUENCE" \
  --signature-policy "$POLICY"

echo "Approving the chaincode definition for Org2"
use_org2
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode approveformyorg \
  "${orderer_flags[@]}" \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --version "$CHAINCODE_VERSION" \
  --package-id "$PACKAGE_ID" \
  --sequence "$CHAINCODE_SEQUENCE" \
  --signature-policy "$POLICY"

echo "Checking commit readiness"
use_org1
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode checkcommitreadiness \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --version "$CHAINCODE_VERSION" \
  --sequence "$CHAINCODE_SEQUENCE" \
  --signature-policy "$POLICY" \
  --output json

echo "Committing the AND(Org1, Org2) chaincode definition"
FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode commit \
  "${orderer_flags[@]}" \
  "${peer_flags[@]}" \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --version "$CHAINCODE_VERSION" \
  --sequence "$CHAINCODE_SEQUENCE" \
  --signature-policy "$POLICY"

FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
  lifecycle chaincode querycommitted \
  --channelID "$CHANNEL_NAME" \
  --name "$CHAINCODE_NAME" \
  --output json

echo "Building and starting the persistent Fabric Gateway bridge"
(
  cd "$GATEWAY_DIR"
  go mod download
  CGO_ENABLED=0 go build \
    -trimpath \
    -ldflags="-s -w" \
    -o "$ARTIFACTS_DIR/fabric-gateway-bridge" \
    .
)
# BEGIN PREPARE GATEWAY SIGNING KEY
if [ ! -f "$GATEWAY_PRIVATE_KEY_HOST_PATH" ]; then
  echo "STOP_GATEWAY_PRIVATE_KEY_NOT_FOUND"
  exit 1
fi

chmod 640 "$GATEWAY_PRIVATE_KEY_HOST_PATH"

export GATEWAY_KEY_GID="$(
  stat -Lc '%g' "$GATEWAY_PRIVATE_KEY_HOST_PATH"
)"

if [ "$(stat -Lc '%a' "$GATEWAY_PRIVATE_KEY_HOST_PATH")" != "640" ]; then
  echo "STOP_GATEWAY_PRIVATE_KEY_MODE"
  exit 1
fi
# END PREPARE GATEWAY SIGNING KEY

compose up -d gateway

echo "Waiting for the Gateway bridge"
for attempt in $(seq 1 30); do
  if curl --fail --silent \
    "http://127.0.0.1:$GATEWAY_BRIDGE_PORT/healthz" \
    >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    compose logs --no-color gateway
    echo "Gateway bridge did not become ready." >&2
    exit 1
  fi
  sleep 2
done

echo "Phase 3 deployment is ready."
echo "Gateway: http://127.0.0.1:$GATEWAY_BRIDGE_PORT"
echo "PostgreSQL: 127.0.0.1:${POSTGRES_PORT:-55433}"
