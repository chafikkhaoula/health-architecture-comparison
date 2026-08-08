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

for required in \
  "$PYTHON" \
  "$SCRIPT_DIR/final.py" \
  "$SCRIPT_DIR/final_environment.sh" \
  "$PROJECT_ROOT/architecture-traditional/.env" \
  "$PROJECT_ROOT/architecture-fabric/.env"; do
  [ -f "$required" ] || {
    echo "Required file is missing: $required" >&2
    exit 1
  }
done

[ -x "$SCRIPT_DIR/final_environment.sh" ] || {
  echo "final_environment.sh is not executable" >&2
  exit 1
}

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP_WORKTREE_NOT_CLEAN" >&2
  git status -sb >&2
  exit 1
fi

mode="${1:-start}"
case "$mode" in
  start)
    commit="$(git rev-parse --short HEAD)"
    batch_id="${2:-final-$(date -u +%Y%m%dT%H%M%SZ)-${commit}}"
    resume_flag=()
    ;;
  resume)
    [ -n "${2:-}" ] || {
      echo "Usage: benchmark/official_final.sh resume BATCH_ID" >&2
      exit 2
    }
    batch_id="$2"
    resume_flag=(--resume)
    ;;
  resume-monitoring)
    [ -n "${2:-}" ] || {
      echo "Usage: benchmark/official_final.sh resume-monitoring BATCH_ID" >&2
      exit 2
    }
    batch_id="$2"
    resume_flag=(--resume --monitoring-only-continuation)
    ;;
  verify)
    [ -n "${2:-}" ] || {
      echo "Usage: benchmark/official_final.sh verify BATCH_ID" >&2
      exit 2
    }
    "$PYTHON" -m benchmark.final \
      --verify-only \
      --batch-id "$2" \
      --output-root results/raw
    exit 0
    ;;
  *)
    echo "Usage: benchmark/official_final.sh {start [BATCH_ID]|resume BATCH_ID|resume-monitoring BATCH_ID|verify BATCH_ID}" >&2
    exit 2
    ;;
esac

printf '\n=== LOCKED FINAL MATRIX ===\n'
printf 'Batch: %s\n' "$batch_id"
printf 'N: 100, 500, 1000\n'
printf 'C: 1, 5, 10\n'
printf 'Repetitions: 10\n'
printf 'Warm-up: one unmeasured run per configuration, then full reset\n'

"$PYTHON" -m benchmark.final \
  --batch-id "$batch_id" \
  "${resume_flag[@]}" \
  --traditional-url http://127.0.0.1:18000 \
  --fabric-url http://127.0.0.1:18001 \
  --workload-sizes 100 500 1000 \
  --concurrencies 1 5 10 \
  --repetitions 10 \
  --seed 42 \
  --timeout-seconds 120 \
  --resource-sampling-interval-seconds 1 \
  --output-root results/raw \
  --driver benchmark/final_environment.sh

printf '\n=== VERIFY FINAL ARTIFACTS ===\n'
"$PYTHON" -m benchmark.final \
  --verify-only \
  --batch-id "$batch_id" \
  --output-root results/raw

printf '\n=== FINAL CLEAN STATUS ===\n'
git log -1 --oneline
git status -sb

printf '\nPHASE4_OFFICIAL_FINAL_OK\n'
