#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TRADITIONAL_ROOT="$PROJECT_ROOT/architecture-traditional"
FABRIC_ROOT="$PROJECT_ROOT/architecture-fabric"
TRADITIONAL_ENV="$TRADITIONAL_ROOT/.env"
FABRIC_ENV="$FABRIC_ROOT/.env"

traditional_compose() {
  docker compose \
    --project-name health-arch-traditional \
    --env-file "$TRADITIONAL_ENV" \
    -f "$TRADITIONAL_ROOT/compose.yaml" \
    "$@"
}

fabric_compose() {
  docker compose \
    --project-name health-arch-fabric \
    --env-file "$FABRIC_ENV" \
    -f "$FABRIC_ROOT/compose.yaml" \
    "$@"
}

require_architecture() {
  case "${1:-}" in
    traditional|fabric) ;;
    *)
      echo "Architecture must be traditional or fabric" >&2
      exit 2
      ;;
  esac
}

preflight() {
  for command_name in docker go python3 curl; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "Required command is missing: $command_name" >&2
      exit 1
    }
  done
  docker compose version >/dev/null
  for required in \
    "$TRADITIONAL_ENV" \
    "$FABRIC_ENV" \
    "$PROJECT_ROOT/.venv/bin/python" \
    "$FABRIC_ROOT/scripts/reset.sh" \
    "$FABRIC_ROOT/scripts/deploy.sh"; do
    [ -e "$required" ] || {
      echo "Required path is missing: $required" >&2
      exit 1
    }
  done
}

stop_architecture() {
  local architecture="$1"
  "$SCRIPT_DIR/services.sh" stop "$architecture" || true
}

teardown_architecture() {
  local architecture="$1"
  stop_architecture "$architecture"
  if [ "$architecture" = traditional ]; then
    traditional_compose down --volumes --remove-orphans
  else
    "$FABRIC_ROOT/scripts/reset.sh"
  fi
}

fresh_start() {
  local architecture="$1"
  stop_architecture "$architecture"

  if [ "$architecture" = traditional ]; then
    traditional_compose down --volumes --remove-orphans
    traditional_compose up -d --wait postgres
  else
    "$FABRIC_ROOT/scripts/reset.sh"
    "$FABRIC_ROOT/scripts/deploy.sh"
    "$FABRIC_ROOT/scripts/status.sh"
  fi

  "$SCRIPT_DIR/services.sh" start "$architecture"
  "$SCRIPT_DIR/services.sh" status "$architecture"
}

fabric_height() {
  # shellcheck source=../architecture-fabric/scripts/common.sh
  source "$FABRIC_ROOT/scripts/common.sh"
  use_org1
  FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
    channel getinfo \
    --channelID "$CHANNEL_NAME" 2>/dev/null |
    python3 -c '
import json
import re
import sys

text = sys.stdin.read()
match = re.search(r"\{.*\}", text, flags=re.DOTALL)
if match is None:
    raise SystemExit("could not parse Fabric channel height")
print(int(json.loads(match.group(0))["height"]))
'
}

fabric_channel_config() {
  # shellcheck source=../architecture-fabric/scripts/common.sh
  source "$FABRIC_ROOT/scripts/common.sh"
  local configtxlator="$FABRIC_BIN_PATH/configtxlator"
  CONFIG_AUDIT_DIR="$(mktemp -d)"
  trap 'rm -rf -- "$CONFIG_AUDIT_DIR"' EXIT
  use_org1
  FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" \
    channel fetch config \
    "$CONFIG_AUDIT_DIR/config.pb" \
    --channelID "$CHANNEL_NAME" \
    "${orderer_flags[@]}" \
    >/dev/null
  "$configtxlator" proto_decode \
    --input "$CONFIG_AUDIT_DIR/config.pb" \
    --type common.Block \
    --output "$CONFIG_AUDIT_DIR/config.json"
  python3 - "$CONFIG_AUDIT_DIR/config.json" <<'PY'
import json
import sys
from pathlib import Path

block = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
values = (
    block["data"]["data"][0]["payload"]["data"]["config"]
    ["channel_group"]["groups"]["Orderer"]["values"]
)
batch_size = values["BatchSize"]["value"]
print(json.dumps({
    "batch_timeout": values["BatchTimeout"]["value"]["timeout"],
    "max_message_count": int(batch_size["max_message_count"]),
    "absolute_max_bytes": int(batch_size["absolute_max_bytes"]),
    "preferred_max_bytes": int(batch_size["preferred_max_bytes"]),
}, sort_keys=True))
PY
}

prime_fabric_chaincode() {
  local gateway_port
  gateway_port="$(
    set -a
    # shellcheck disable=SC1090
    source "$FABRIC_ENV"
    set +a
    printf '%s' "${GATEWAY_BRIDGE_PORT:-18081}"
  )"
  curl --silent --show-error --max-time 30 \
    -H 'Content-Type: application/json' \
    --data '{"function":"GetAudit","arguments":["Patient","benchmark-prime-missing"]}' \
    "http://127.0.0.1:$gateway_port/v1/evaluate" \
    >/dev/null

  # The Gateway evaluate path warms Org1. Issue the same read-only miss
  # directly to Org2 so both endorsement-side chaincode containers are
  # already resident before the measured boundary.
  # shellcheck source=../architecture-fabric/scripts/common.sh
  source "$FABRIC_ROOT/scripts/common.sh"
  use_org2
  FABRIC_CFG_PATH="$FABRIC_CFG_PATH" "$PEER" chaincode query \
    --channelID "$CHANNEL_NAME" \
    --name "$CHAINCODE_NAME" \
    --ctor '{"Args":["GetAudit","Patient","benchmark-prime-missing"]}' \
    >/dev/null 2>&1 || true

  for attempt in $(seq 1 30); do
    chaincode_count="$(
      docker ps \
        --filter network=health-arch-fabric-net \
        --format '{{.Names}}' |
        grep -Ec '^dev-peer' || true
    )"
    if [ "$chaincode_count" -ge 2 ]; then
      echo "Both Fabric chaincode containers are warm"
      return
    fi
    sleep 1
  done
  echo "Fabric chaincode container did not start" >&2
  exit 1
}

capture_logs() {
  local architecture="$1"
  "$SCRIPT_DIR/services.sh" logs "$architecture" 2>&1 || true
  if [ "$architecture" = traditional ]; then
    traditional_compose logs --no-color --timestamps 2>&1 || true
  else
    fabric_compose logs --no-color --timestamps 2>&1 || true
  fi
}

cd "$PROJECT_ROOT"

case "${1:-}" in
  preflight)
    preflight
    ;;
  fresh-start)
    require_architecture "${2:-}"
    fresh_start "$2"
    ;;
  teardown)
    require_architecture "${2:-}"
    teardown_architecture "$2"
    ;;
  stop)
    require_architecture "${2:-}"
    stop_architecture "$2"
    ;;
  stop-all)
    teardown_architecture traditional
    teardown_architecture fabric
    ;;
  height)
    [ "${2:-}" = fabric ] || {
      echo "height is available only for fabric" >&2
      exit 2
    }
    fabric_height
    ;;
  prime)
    [ "${2:-}" = fabric ] || {
      echo "prime is available only for fabric" >&2
      exit 2
    }
    prime_fabric_chaincode
    ;;
  channel-config)
    [ "${2:-}" = fabric ] || {
      echo "channel-config is available only for fabric" >&2
      exit 2
    }
    fabric_channel_config
    ;;
  logs)
    require_architecture "${2:-}"
    capture_logs "$2"
    ;;
  *)
    echo "Usage: benchmark/final_environment.sh {preflight|fresh-start|stop|teardown|stop-all|height|prime|channel-config|logs} [architecture]" >&2
    exit 2
    ;;
esac
