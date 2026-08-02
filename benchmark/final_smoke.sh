#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"

cleanup() {
  local exit_code=$?
  trap - EXIT
  "$SCRIPT_DIR/final_environment.sh" stop-all >/dev/null 2>&1 || true
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

cd "$PROJECT_ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP_WORKTREE_NOT_CLEAN" >&2
  git status -sb >&2
  exit 1
fi

commit="$(git rev-parse --short HEAD)"
batch_id="${1:-final-smoke-$(date -u +%Y%m%dT%H%M%SZ)-${commit}}"

"$PYTHON" -m benchmark.final \
  --smoke \
  --batch-id "$batch_id" \
  --traditional-url http://127.0.0.1:18000 \
  --fabric-url http://127.0.0.1:18001 \
  --workload-sizes 10 \
  --concurrencies 2 \
  --repetitions 1 \
  --seed 42 \
  --timeout-seconds 120 \
  --resource-sampling-interval-seconds 1 \
  --output-root results/pilot \
  --driver benchmark/final_environment.sh

"$PYTHON" -m benchmark.final \
  --verify-only \
  --batch-id "$batch_id" \
  --output-root results/pilot

printf '\nFINAL_ORCHESTRATION_SMOKE_OK\n'
