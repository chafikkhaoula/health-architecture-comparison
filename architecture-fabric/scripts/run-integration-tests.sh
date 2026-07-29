#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

cd "$PROJECT_ROOT"

export RUN_FABRIC_INTEGRATION=1
export FABRIC_GATEWAY_URL="http://127.0.0.1:$GATEWAY_BRIDGE_PORT"
export FABRIC_POSTGRES_HOST=127.0.0.1
export FABRIC_POSTGRES_PORT="${POSTGRES_PORT:-55433}"

.venv/bin/python -m pytest \
  -q \
  tests/test_fabric_integration.py
