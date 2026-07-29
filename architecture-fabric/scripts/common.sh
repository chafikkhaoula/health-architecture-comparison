#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd -- "$FABRIC_ROOT/.." && pwd)"
NETWORK_DIR="$FABRIC_ROOT/network"
ORGANIZATIONS_DIR="$NETWORK_DIR/organizations"
ARTIFACTS_DIR="$NETWORK_DIR/artifacts"
CHAINCODE_DIR="$FABRIC_ROOT/chaincode"
GATEWAY_DIR="$FABRIC_ROOT/gateway"
ENV_FILE="$FABRIC_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE; copy .env.example and replace secrets." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${FABRIC_BIN_PATH:=/home/khcha/projects/fabric-samples/bin}"
: "${FABRIC_CFG_PATH:=/home/khcha/projects/fabric-samples/config}"
: "${FABRIC_VERSION:=2.5.15}"
: "${CHANNEL_NAME:=healthchannel}"
: "${CHAINCODE_NAME:=healthrecords}"
: "${CHAINCODE_VERSION:=1.0}"
: "${CHAINCODE_SEQUENCE:=1}"
: "${ORDERER_PORT:=27050}"
: "${ORDERER_ADMIN_PORT:=27053}"
: "${ORDERER_OPERATIONS_PORT:=29443}"
: "${ORG1_PEER_PORT:=27051}"
: "${ORG1_OPERATIONS_PORT:=29444}"
: "${ORG2_PEER_PORT:=29051}"
: "${ORG2_OPERATIONS_PORT:=29445}"
: "${GATEWAY_BRIDGE_PORT:=18081}"

export \
  FABRIC_BIN_PATH \
  FABRIC_CFG_PATH \
  FABRIC_VERSION \
  CHANNEL_NAME \
  CHAINCODE_NAME \
  CHAINCODE_VERSION \
  CHAINCODE_SEQUENCE \
  ORDERER_PORT \
  ORDERER_ADMIN_PORT \
  ORDERER_OPERATIONS_PORT \
  ORG1_PEER_PORT \
  ORG1_OPERATIONS_PORT \
  ORG2_PEER_PORT \
  ORG2_OPERATIONS_PORT \
  GATEWAY_BRIDGE_PORT

CRYPTOGEN="$FABRIC_BIN_PATH/cryptogen"
CONFIGTXGEN="$FABRIC_BIN_PATH/configtxgen"
OSNADMIN="$FABRIC_BIN_PATH/osnadmin"
PEER="$FABRIC_BIN_PATH/peer"

ORDERER_CA="$ORGANIZATIONS_DIR/ordererOrganizations/hac.example.com/orderers/orderer.hac.example.com/msp/tlscacerts/tlsca.hac.example.com-cert.pem"
ORDERER_TLS_CA="$ORGANIZATIONS_DIR/ordererOrganizations/hac.example.com/orderers/orderer.hac.example.com/tls/ca.crt"
ORDERER_TLS_CERT="$ORGANIZATIONS_DIR/ordererOrganizations/hac.example.com/orderers/orderer.hac.example.com/tls/server.crt"
ORDERER_TLS_KEY="$ORGANIZATIONS_DIR/ordererOrganizations/hac.example.com/orderers/orderer.hac.example.com/tls/server.key"
ORG1_TLS_CA="$ORGANIZATIONS_DIR/peerOrganizations/org1.hac.example.com/peers/peer0.org1.hac.example.com/tls/ca.crt"
ORG2_TLS_CA="$ORGANIZATIONS_DIR/peerOrganizations/org2.hac.example.com/peers/peer0.org2.hac.example.com/tls/ca.crt"

compose() {
  docker compose \
    --project-name health-arch-fabric \
    --env-file "$ENV_FILE" \
    -f "$FABRIC_ROOT/compose.yaml" \
    "$@"
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
}

require_executable() {
  local path="$1"
  if [ ! -x "$path" ]; then
    echo "Required executable is missing: $path" >&2
    exit 1
  fi
}

use_org1() {
  export CORE_PEER_TLS_ENABLED=true
  export CORE_PEER_LOCALMSPID=Org1MSP
  export CORE_PEER_MSPCONFIGPATH="$ORGANIZATIONS_DIR/peerOrganizations/org1.hac.example.com/users/Admin@org1.hac.example.com/msp"
  export CORE_PEER_ADDRESS="127.0.0.1:$ORG1_PEER_PORT"
  export CORE_PEER_TLS_ROOTCERT_FILE="$ORG1_TLS_CA"
  unset CORE_PEER_TLS_SERVERHOSTOVERRIDE
}

use_org2() {
  export CORE_PEER_TLS_ENABLED=true
  export CORE_PEER_LOCALMSPID=Org2MSP
  export CORE_PEER_MSPCONFIGPATH="$ORGANIZATIONS_DIR/peerOrganizations/org2.hac.example.com/users/Admin@org2.hac.example.com/msp"
  export CORE_PEER_ADDRESS="127.0.0.1:$ORG2_PEER_PORT"
  export CORE_PEER_TLS_ROOTCERT_FILE="$ORG2_TLS_CA"
  unset CORE_PEER_TLS_SERVERHOSTOVERRIDE
}

orderer_flags=(
  --orderer "127.0.0.1:$ORDERER_PORT"
  --tls
  --cafile "$ORDERER_TLS_CA"
  --ordererTLSHostnameOverride orderer.hac.example.com
)

peer_flags=(
  --peerAddresses "127.0.0.1:$ORG1_PEER_PORT"
  --tlsRootCertFiles "$ORG1_TLS_CA"
  --peerAddresses "127.0.0.1:$ORG2_PEER_PORT"
  --tlsRootCertFiles "$ORG2_TLS_CA"
)

# BEGIN GATEWAY SIGNING KEY ACCESS
# Keep the image's non-root user and expose only User1's signing key
# through its host group.
GATEWAY_PRIVATE_KEY_HOST_PATH="$ORGANIZATIONS_DIR/peerOrganizations/org1.hac.example.com/users/User1@org1.hac.example.com/msp/keystore/priv_sk"

if [ -e "$GATEWAY_PRIVATE_KEY_HOST_PATH" ]; then
  export GATEWAY_KEY_GID="$(
    stat -Lc '%g' "$GATEWAY_PRIVATE_KEY_HOST_PATH"
  )"
else
  export GATEWAY_KEY_GID="$(id -g)"
fi
# END GATEWAY SIGNING KEY ACCESS
