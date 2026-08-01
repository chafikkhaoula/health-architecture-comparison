#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
TRADITIONAL_ROOT="$PROJECT_ROOT/architecture-traditional"
FABRIC_ROOT="$PROJECT_ROOT/architecture-fabric"
TRADITIONAL_ENV="$TRADITIONAL_ROOT/.env"
FABRIC_ENV="$FABRIC_ROOT/.env"
SERVICES_STARTED=false

cleanup() {
  local exit_code=$?
  trap - EXIT

  if [ "$SERVICES_STARTED" = true ]; then
    "$PROJECT_ROOT/benchmark/services.sh" stop || true
  fi

  exit "$exit_code"
}
trap cleanup EXIT

cd "$PROJECT_ROOT"

for required in \
  "$TRADITIONAL_ENV" \
  "$FABRIC_ENV" \
  "$PROJECT_ROOT/.venv/bin/python" \
  "$PROJECT_ROOT/benchmark/pilot.py" \
  "$PROJECT_ROOT/architecture-traditional/app/main.py" \
  "$PROJECT_ROOT/architecture-fabric/app/main.py"; do
  if [ ! -f "$required" ]; then
    echo "Required file is missing: $required" >&2
    exit 1
  fi
done

FABRIC_GATEWAY_PORT="$(
  set -a
  # shellcheck disable=SC1090
  source "$FABRIC_ENV"
  set +a
  printf '%s' "${GATEWAY_BRIDGE_PORT:-18081}"
)"

if [ -n "$(git status --porcelain)" ]; then
  echo "STOP_WORKTREE_NOT_CLEAN" >&2
  git status -sb >&2
  exit 1
fi

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

printf '\n=== START TRADITIONAL POSTGRESQL ===\n'
traditional_compose up -d --wait postgres

printf '\n=== ENSURE FABRIC INFRASTRUCTURE ===\n'
if "$FABRIC_ROOT/scripts/status.sh" >/dev/null 2>&1; then
  echo "Existing Fabric deployment is ready"
elif [ -d "$FABRIC_ROOT/network/organizations" ] && \
  [ -x "$FABRIC_ROOT/network/artifacts/fabric-gateway-bridge" ]; then
  echo "Starting the existing Fabric deployment"
  fabric_compose up -d \
    orderer peer0org1 peer0org2 postgres gateway

  gateway_ready=false
  for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error \
      --max-time 3 \
      "http://127.0.0.1:$FABRIC_GATEWAY_PORT/healthz" \
      >/dev/null 2>&1; then
      gateway_ready=true
      break
    fi
    sleep 2
  done

  if [ "$gateway_ready" != true ]; then
    echo "STOP_FABRIC_GATEWAY_NOT_READY" >&2
    fabric_compose logs --no-color --tail=100 gateway >&2
    exit 1
  fi

  if ! "$FABRIC_ROOT/scripts/status.sh"; then
    echo "STOP_EXISTING_FABRIC_STATE_INVALID" >&2
    exit 1
  fi
elif [ ! -e "$FABRIC_ROOT/network/organizations" ] && \
  [ ! -e "$FABRIC_ROOT/network/artifacts" ]; then
  echo "Creating a clean Fabric deployment"
  "$FABRIC_ROOT/scripts/deploy.sh"
  "$FABRIC_ROOT/scripts/status.sh"
else
  echo "STOP_PARTIAL_FABRIC_STATE" >&2
  echo "Generated Fabric state is incomplete; no reset was performed." >&2
  exit 1
fi

printf '\n=== START EXISTING HTTP BOUNDARY ===\n'
SERVICES_STARTED=true
"$PROJECT_ROOT/benchmark/services.sh" start
"$PROJECT_ROOT/benchmark/services.sh" status

pilot_commit="$(git rev-parse --short HEAD)"
batch_id="${1:-pilot-$(date -u +%Y%m%dT%H%M%SZ)-${pilot_commit}}"

printf '\n=== RUN OFFICIAL PILOT ===\n'
printf 'Batch: %s\n' "$batch_id"

"$PROJECT_ROOT/.venv/bin/python" -m benchmark.pilot \
  --batch-id "$batch_id" \
  --traditional-url http://127.0.0.1:18000 \
  --fabric-url http://127.0.0.1:18001 \
  --workload-size 10 \
  --concurrency 2 \
  --repetitions 1 \
  --seed 42 \
  --timeout-seconds 120 \
  --output-root results/pilot

printf '\n=== VALIDATE PILOT ARTIFACTS ===\n'

"$PROJECT_ROOT/.venv/bin/python" - "$batch_id" <<'PY'
import csv
import json
import sys
from pathlib import Path

batch_id = sys.argv[1]
output_dir = Path("results/pilot") / batch_id
manifest = json.loads(
    (output_dir / "manifest.json").read_text(encoding="utf-8")
)

with (output_dir / "requests.csv").open(
    newline="",
    encoding="utf-8",
) as handle:
    requests = list(csv.DictReader(handle))

with (output_dir / "runs.csv").open(
    newline="",
    encoding="utf-8",
) as handle:
    runs = list(csv.DictReader(handle))

assert manifest["pilot"] is True
assert manifest["status"] == "completed"
assert manifest["git_dirty"] is False
assert manifest["workload_size"] == 10
assert manifest["concurrency"] == 2
assert manifest["repetitions"] == 1
assert manifest["seed"] == 42
assert len(requests) == 120, len(requests)
assert len(runs) == 12, len(runs)
assert all(
    row["correctness_result"].lower() == "true"
    for row in requests
)

print(f"OUTPUT_DIR={output_dir}")
print(f"MEASURED_REQUESTS={len(requests)}")
print(f"MEASURED_RUNS={len(runs)}")
print("PILOT_ARTIFACTS_VALID")
PY

printf '\n=== STOP MANAGED HTTP SERVICES ===\n'
"$PROJECT_ROOT/benchmark/services.sh" stop
SERVICES_STARTED=false

printf '\n=== FINAL STATUS ===\n'
git log -1 --oneline
git status -sb

printf '\nPHASE4_OFFICIAL_PILOT_OK\n'
