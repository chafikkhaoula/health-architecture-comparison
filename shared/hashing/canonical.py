from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest

import rfc8785
from pydantic import JsonValue


def canonical_json_bytes(payload: JsonValue) -> bytes:
    """Serialize a JSON value using RFC 8785."""
    return rfc8785.dumps(payload)


def payload_sha256(payload: JsonValue) -> str:
    """Return the lowercase SHA-256 digest of canonical JSON bytes."""
    return sha256(canonical_json_bytes(payload)).hexdigest()


def verify_payload_sha256(payload: JsonValue, expected_hash: str) -> bool:
    """Compare a payload digest with an expected SHA-256 digest."""
    if len(expected_hash) != 64:
        return False
    return compare_digest(payload_sha256(payload), expected_hash)
