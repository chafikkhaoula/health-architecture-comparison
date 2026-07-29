#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

record_id="endorsement-test-$(date -u +%Y%m%d%H%M%S)"
payload_hash="$(
  printf '%s' "$record_id" | sha256sum | cut -d' ' -f1
)"
submit_request="$(mktemp)"
submit_response="$(mktemp)"
query_request="$(mktemp)"
before_response="$(mktemp)"
after_response="$(mktemp)"
trap 'rm -f -- "$submit_request" "$submit_response" "$query_request" "$before_response" "$after_response"' EXIT

python3 - \
  "$submit_request" \
  "$query_request" \
  "$record_id" \
  "$payload_hash" <<'PY'
import json
import sys

submit_path, query_path, record_id, payload_hash = sys.argv[1:]
with open(submit_path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "function": "CreateRecord",
            "arguments": [
                "Patient",
                record_id,
                payload_hash,
                "endorsement-tester",
                "org-001",
            ],
            "endorsing_organizations": ["Org1MSP"],
        },
        handle,
        separators=(",", ":"),
    )
with open(query_path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "function": "GetRecordEvidence",
            "arguments": ["Patient", record_id],
        },
        handle,
        separators=(",", ":"),
    )
PY

before_status="$(
  curl \
    --silent \
    --show-error \
    --output "$before_response" \
    --write-out '%{http_code}' \
    --header 'Content-Type: application/json' \
    --data-binary "@$query_request" \
    "http://127.0.0.1:$GATEWAY_BRIDGE_PORT/v1/evaluate"
)"

printf 'record_id=%s\n' "$record_id"
printf 'world_state_before_status=%s\n' "$before_status"
python3 -m json.tool "$before_response"
if [ "$before_status" -ne 404 ]; then
  echo "The unique test key was unexpectedly present before the attempt." >&2
  exit 1
fi

submit_status="$(
  curl \
    --silent \
    --show-error \
    --output "$submit_response" \
    --write-out '%{http_code}' \
    --header 'Content-Type: application/json' \
    --data-binary "@$submit_request" \
    "http://127.0.0.1:$GATEWAY_BRIDGE_PORT/v1/submit"
)"

printf 'incomplete_endorsement_status=%s\n' "$submit_status"
python3 -m json.tool "$submit_response"
if [ "$submit_status" -lt 400 ]; then
  echo "Expected the incomplete endorsement request to fail." >&2
  exit 1
fi

after_status="$(
  curl \
    --silent \
    --show-error \
    --output "$after_response" \
    --write-out '%{http_code}' \
    --header 'Content-Type: application/json' \
    --data-binary "@$query_request" \
    "http://127.0.0.1:$GATEWAY_BRIDGE_PORT/v1/evaluate"
)"

printf 'world_state_after_status=%s\n' "$after_status"
python3 -m json.tool "$after_response"
if [ "$after_status" -ne 404 ]; then
  echo "Protected world state changed or could not be verified." >&2
  exit 1
fi

echo "Incomplete endorsement was rejected and protected state stayed absent."
