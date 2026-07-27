import pytest
import rfc8785

from shared.hashing.canonical import (
    canonical_json_bytes,
    payload_sha256,
    verify_payload_sha256,
)


def test_canonicalization_is_independent_of_key_order() -> None:
    first = {"b": 2, "a": 1}
    second = {"a": 1, "b": 2}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_bytes(first) == b'{"a":1,"b":2}'


def test_known_sha256_vector() -> None:
    assert payload_sha256({"b": 2, "a": 1}) == (
        "43258cff783fe7036d8a43033f830adfc"
        "60ec037382473548ac742b888292777"
    )


def test_verification_detects_changed_payload() -> None:
    original = {
        "resourceType": "Patient",
        "id": "patient-000001",
    }
    changed = {
        "resourceType": "Patient",
        "id": "patient-000002",
    }
    digest = payload_sha256(original)

    assert verify_payload_sha256(original, digest)
    assert not verify_payload_sha256(changed, digest)


@pytest.mark.parametrize(
    "invalid_hash",
    ["", "0" * 63, "0" * 65],
)
def test_verification_rejects_invalid_digest_length(
    invalid_hash: str,
) -> None:
    assert not verify_payload_sha256({"a": 1}, invalid_hash)


def test_canonicalization_rejects_non_string_mapping_keys() -> None:
    with pytest.raises(rfc8785.CanonicalizationError):
        canonical_json_bytes({1: "invalid"})
