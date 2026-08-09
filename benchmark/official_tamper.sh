#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
DRIVER="$SCRIPT_DIR/final_environment.sh"
RQ1_BATCH_ID="final-20260805T122631Z-11a3fbb"
EXPECTED_BRANCH="phase-3-fabric-adapter"
ENVIRONMENT_STARTED=false

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  if [ "$ENVIRONMENT_STARTED" = true ]; then
    if [ "$exit_code" -ne 0 ]; then
      "$DRIVER" logs traditional || true
      "$DRIVER" logs fabric || true
    fi
    "$DRIVER" stop-all >/dev/null 2>&1 || true
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

cd "$PROJECT_ROOT"

for required in \
  "$PYTHON" \
  "$DRIVER" \
  "$SCRIPT_DIR/tamper.py" \
  "$PROJECT_ROOT/docs/experimental_protocol.md" \
  "$PROJECT_ROOT/docs/rq2_tamper_execution.md" \
  "$PROJECT_ROOT/architecture-traditional/.env" \
  "$PROJECT_ROOT/architecture-fabric/.env"; do
  [ -f "$required" ] || {
    echo "Required file is missing: $required" >&2
    exit 1
  }
done

mode="${1:-pilot}"
case "$mode" in
  pilot)
    trials=1
    output_root="results/pilot/rq2-tamper"
    mode_flag=(--pilot)
    batch_prefix="tamper-pilot"
    ;;
  start)
    trials=10
    output_root="results/raw/rq2-tamper"
    mode_flag=()
    batch_prefix="tamper-final"
    ;;
  *)
    echo "Usage: benchmark/official_tamper.sh {pilot|start} [BATCH_ID]" >&2
    exit 2
    ;;
esac

branch="$(git branch --show-current)"
if [ "$branch" != "$EXPECTED_BRANCH" ]; then
  echo "STOP_UNEXPECTED_BRANCH=$branch" >&2
  echo "EXPECTED_BRANCH=$EXPECTED_BRANCH" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP_WORKTREE_NOT_CLEAN" >&2
  git status -sb >&2
  exit 1
fi

rq1_dir="$PROJECT_ROOT/results/raw/$RQ1_BATCH_ID"
if [ ! -f "$rq1_dir/SHA256SUMS" ]; then
  echo "STOP_FROZEN_RQ1_BATCH_NOT_FOUND=$rq1_dir" >&2
  exit 1
fi
if find "$rq1_dir" -perm /222 -print -quit | grep -q .; then
  echo "STOP_FROZEN_RQ1_BATCH_IS_WRITABLE" >&2
  exit 1
fi

printf '\n=== VERIFY FROZEN RQ1 REFERENCE ===\n'
"$PYTHON" -m benchmark.final \
  --verify-only \
  --batch-id "$RQ1_BATCH_ID" \
  --output-root results/raw
rq1_before="$(sha256sum "$rq1_dir/SHA256SUMS" | cut -d' ' -f1)"

"$DRIVER" preflight
"$DRIVER" stop-all
ENVIRONMENT_STARTED=true

printf '\n=== FRESH TRADITIONAL ENVIRONMENT ===\n'
"$DRIVER" fresh-start traditional

printf '\n=== FRESH FABRIC ENVIRONMENT ===\n'
"$DRIVER" fresh-start fabric
"$DRIVER" prime fabric

load_database_environment() {
  local prefix="$1"
  local env_file="$2"

  unset \
    POSTGRES_DB \
    POSTGRES_ADMIN_USER \
    POSTGRES_ADMIN_PASSWORD \
    POSTGRES_PORT \
    APP_DB_USER \
    APP_DB_PASSWORD \
    GATEWAY_BRIDGE_PORT
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a

  : "${POSTGRES_DB:?POSTGRES_DB is required}"
  : "${POSTGRES_ADMIN_USER:?POSTGRES_ADMIN_USER is required}"
  : "${POSTGRES_ADMIN_PASSWORD:?POSTGRES_ADMIN_PASSWORD is required}"
  : "${POSTGRES_PORT:?POSTGRES_PORT is required}"

  export "RQ2_${prefix}_POSTGRES_DB=$POSTGRES_DB"
  export "RQ2_${prefix}_POSTGRES_PORT=$POSTGRES_PORT"
  export "RQ2_${prefix}_ADMIN_USER=$POSTGRES_ADMIN_USER"
  export "RQ2_${prefix}_ADMIN_PASSWORD=$POSTGRES_ADMIN_PASSWORD"
}

load_database_environment \
  TRADITIONAL \
  "$PROJECT_ROOT/architecture-traditional/.env"
load_database_environment \
  FABRIC \
  "$PROJECT_ROOT/architecture-fabric/.env"

set -a
# shellcheck disable=SC1091
source "$PROJECT_ROOT/architecture-fabric/.env"
set +a
fabric_gateway_port="${GATEWAY_BRIDGE_PORT:-18081}"
unset \
  POSTGRES_DB \
  POSTGRES_ADMIN_USER \
  POSTGRES_ADMIN_PASSWORD \
  POSTGRES_PORT \
  APP_DB_USER \
  APP_DB_PASSWORD \
  GATEWAY_BRIDGE_PORT

commit="$(git rev-parse --short HEAD)"
batch_id="${2:-${batch_prefix}-$(date -u +%Y%m%dT%H%M%SZ)-${commit}}"

printf '\n=== RUN SEPARATE RQ2 TAMPER EXPERIMENT ===\n'
printf 'Mode: %s\n' "$mode"
printf 'Batch: %s\n' "$batch_id"
printf 'Scenario cells: 8\n'
printf 'Trials per scenario: %s\n' "$trials"
printf 'Expected rows: %s\n' "$((8 * trials))"

"$PYTHON" -m benchmark.tamper \
  --batch-id "$batch_id" \
  --rq1-batch-id "$RQ1_BATCH_ID" \
  --traditional-url http://127.0.0.1:18000 \
  --fabric-url http://127.0.0.1:18001 \
  --fabric-gateway-url "http://127.0.0.1:$fabric_gateway_port" \
  --trials "$trials" \
  --seed 4202 \
  --timeout-seconds 120 \
  --output-root "$output_root" \
  "${mode_flag[@]}"

printf '\n=== VERIFY RQ2 ARTIFACTS ===\n'
"$PYTHON" -m benchmark.tamper \
  --verify-only \
  --batch-id "$batch_id" \
  --rq1-batch-id "$RQ1_BATCH_ID" \
  --output-root "$output_root"

printf '\n=== REVERIFY FROZEN RQ1 REFERENCE ===\n'
"$PYTHON" -m benchmark.final \
  --verify-only \
  --batch-id "$RQ1_BATCH_ID" \
  --output-root results/raw
rq1_after="$(sha256sum "$rq1_dir/SHA256SUMS" | cut -d' ' -f1)"
if [ "$rq1_before" != "$rq1_after" ]; then
  echo "STOP_RQ1_REFERENCE_CHANGED" >&2
  exit 1
fi

printf '\n=== CLEANUP ISOLATED EXPERIMENT ENVIRONMENT ===\n'
"$DRIVER" stop-all
ENVIRONMENT_STARTED=false

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP_WORKTREE_CHANGED_DURING_RQ2" >&2
  git status -sb >&2
  exit 1
fi

printf '\nRQ2_TAMPER_%s_OK\n' "${mode^^}"
printf 'OUTPUT_DIR=%s/%s\n' "$output_root" "$batch_id"
printf 'RQ1_REFERENCE_SHA256=%s\n' "$rq1_after"
