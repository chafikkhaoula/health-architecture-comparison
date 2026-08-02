#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
RUNTIME_DIR="$PROJECT_ROOT/tmp/benchmark-services"

require_file() {
  local path="$1"
  if [ ! -f "$path" ]; then
    echo "Required file is missing: $path" >&2
    exit 1
  fi
}

health_matches() {
  local architecture="$1"
  local port="$2"

  curl --fail --silent --show-error \
    --max-time 3 \
    "http://127.0.0.1:$port/healthz" 2>/dev/null |
    "$PYTHON" -c '
import json
import sys

expected = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)

raise SystemExit(
    0
    if payload.get("status") == "ok"
    and payload.get("architecture") == expected
    else 1
)
' "$architecture"
}

start_one() {
  local architecture="$1"
  local port="$2"
  local env_file="$3"
  local app_dir="$4"
  local pid_file="$RUNTIME_DIR/$architecture.pid"
  local log_file="$RUNTIME_DIR/$architecture.log"

  if health_matches "$architecture" "$port"; then
    echo "$architecture API already healthy on port $port"
    return
  fi

  if [ -f "$pid_file" ]; then
    local stale_pid
    stale_pid="$(<"$pid_file")"
    if [[ "$stale_pid" =~ ^[0-9]+$ ]] && \
      kill -0 "$stale_pid" 2>/dev/null; then
      echo "$architecture process $stale_pid exists but is unhealthy" >&2
      echo "See $log_file" >&2
      exit 1
    fi
    rm -f -- "$pid_file"
  fi

  (
    set -Eeuo pipefail
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a

    if [ "$architecture" = "traditional" ]; then
      export TRADITIONAL_POSTGRES_PORT="${POSTGRES_PORT:-55432}"
    else
      export FABRIC_POSTGRES_PORT="${POSTGRES_PORT:-55433}"
      export FABRIC_GATEWAY_URL="http://127.0.0.1:${GATEWAY_BRIDGE_PORT:-18081}"
    fi

    export PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
    cd "$PROJECT_ROOT"

    nohup "$PYTHON" -m uvicorn app.main:app \
      --app-dir "$app_dir" \
      --host 127.0.0.1 \
      --port "$port" \
      --log-level warning \
      >"$log_file" 2>&1 &
    printf '%s\n' "$!" >"$pid_file"
  )

  for attempt in $(seq 1 60); do
    if health_matches "$architecture" "$port"; then
      echo "$architecture API healthy on port $port"
      return
    fi

    local pid
    pid="$(<"$pid_file")"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$architecture API exited during startup" >&2
      tail -n 80 "$log_file" >&2 || true
      exit 1
    fi
    sleep 1
  done

  echo "$architecture API did not become healthy" >&2
  tail -n 80 "$log_file" >&2 || true
  exit 1
}

stop_one() {
  local architecture="$1"
  local app_dir="$2"
  local pid_file="$RUNTIME_DIR/$architecture.pid"

  if [ ! -f "$pid_file" ]; then
    echo "$architecture API has no managed PID"
    return
  fi

  local pid
  pid="$(<"$pid_file")"
  if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
    echo "Refusing invalid PID in $pid_file" >&2
    exit 1
  fi

  if kill -0 "$pid" 2>/dev/null; then
    local command_line
    command_line="$(tr '\0' ' ' <"/proc/$pid/cmdline")"
    if [[ "$command_line" != *"uvicorn app.main:app"* ]] || \
      [[ "$command_line" != *"$app_dir"* ]]; then
      echo "Refusing to stop unrelated process $pid" >&2
      exit 1
    fi

    kill "$pid"
    for attempt in $(seq 1 30); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 1
    done

    if kill -0 "$pid" 2>/dev/null; then
      echo "Process $pid did not stop cleanly" >&2
      exit 1
    fi
  fi

  rm -f -- "$pid_file"
  echo "$architecture API stopped"
}

status_one() {
  local architecture="$1"
  local port="$2"

  printf '%s: ' "$architecture"
  if health_matches "$architecture" "$port"; then
    curl --fail --silent --show-error \
      "http://127.0.0.1:$port/healthz"
    printf '\n'
  else
    printf 'unavailable\n'
    return 1
  fi
}

mkdir -p "$RUNTIME_DIR"
require_file "$PYTHON"
require_file "$PROJECT_ROOT/architecture-traditional/.env"
require_file "$PROJECT_ROOT/architecture-fabric/.env"
require_file "$PROJECT_ROOT/architecture-traditional/app/main.py"
require_file "$PROJECT_ROOT/architecture-fabric/app/main.py"

case "${1:-}" in
  start)
    case "${2:-all}" in
      traditional)
        start_one \
          traditional \
          18000 \
          "$PROJECT_ROOT/architecture-traditional/.env" \
          "$PROJECT_ROOT/architecture-traditional"
        ;;
      fabric)
        start_one \
          fabric \
          18001 \
          "$PROJECT_ROOT/architecture-fabric/.env" \
          "$PROJECT_ROOT/architecture-fabric"
        ;;
      all)
        start_one \
          traditional \
          18000 \
          "$PROJECT_ROOT/architecture-traditional/.env" \
          "$PROJECT_ROOT/architecture-traditional"
        if ! start_one \
          fabric \
          18001 \
          "$PROJECT_ROOT/architecture-fabric/.env" \
          "$PROJECT_ROOT/architecture-fabric"; then
          stop_one \
            traditional \
            "$PROJECT_ROOT/architecture-traditional" || true
          exit 1
        fi
        ;;
      *)
        echo "Architecture must be traditional or fabric" >&2
        exit 2
        ;;
    esac
    ;;
  stop)
    case "${2:-all}" in
      traditional)
        stop_one \
          traditional \
          "$PROJECT_ROOT/architecture-traditional"
        ;;
      fabric)
        stop_one \
          fabric \
          "$PROJECT_ROOT/architecture-fabric"
        ;;
      all)
        stop_one \
          traditional \
          "$PROJECT_ROOT/architecture-traditional"
        stop_one \
          fabric \
          "$PROJECT_ROOT/architecture-fabric"
        ;;
      *)
        echo "Architecture must be traditional or fabric" >&2
        exit 2
        ;;
    esac
    ;;
  status)
    case "${2:-all}" in
      traditional) status_one traditional 18000 ;;
      fabric) status_one fabric 18001 ;;
      all)
        status_one traditional 18000
        status_one fabric 18001
        ;;
      *)
        echo "Architecture must be traditional or fabric" >&2
        exit 2
        ;;
    esac
    ;;
  logs)
    case "${2:-all}" in
      traditional|fabric)
        tail -n 200 "$RUNTIME_DIR/${2}.log"
        ;;
      all)
        tail -n 200 \
          "$RUNTIME_DIR/traditional.log" \
          "$RUNTIME_DIR/fabric.log"
        ;;
      *)
        echo "Architecture must be traditional or fabric" >&2
        exit 2
        ;;
    esac
    ;;
  *)
    echo "Usage: benchmark/services.sh {start|stop|status|logs} [traditional|fabric]" >&2
    exit 2
    ;;
esac
