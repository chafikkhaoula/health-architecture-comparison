from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timezone
from hashlib import sha256
from hmac import compare_digest

from shared.hashing.canonical import canonical_json_bytes
from shared.schemas.operations import AuditEvent


@dataclass(frozen=True, slots=True)
class StoredAuditLink:
    event: AuditEvent
    previous_hash: str | None
    event_hash: str


def _validate_digest(value: str | None, field_name: str) -> None:
    if value is None:
        return

    if len(value) != 64 or any(
        character not in "0123456789abcdef"
        for character in value
    ):
        raise ValueError(
            f"{field_name} must be a lowercase SHA-256 digest"
        )


def _canonical_timestamp(event: AuditEvent) -> str:
    return (
        event.timestamp
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def audit_event_preimage(
    event: AuditEvent,
    previous_hash: str | None,
) -> dict[str, object]:
    """Return the versioned canonical preimage for an audit event."""
    _validate_digest(previous_hash, "previous_hash")

    return {
        "chain_version": "audit-chain-v1",
        "event_id": event.event_id,
        "sequence": event.sequence,
        "resource_type": event.record.resource_type.value,
        "resource_id": event.record.resource_id,
        "actor_id": event.actor.actor_id,
        "organization_id": event.actor.organization_id,
        "action": event.action.value,
        "decision": (
            event.decision.value
            if event.decision is not None
            else None
        ),
        "timestamp": _canonical_timestamp(event),
        "previous_hash": previous_hash,
    }


def audit_event_sha256(
    event: AuditEvent,
    previous_hash: str | None,
) -> str:
    """Hash an audit event using RFC 8785 and SHA-256."""
    preimage = audit_event_preimage(event, previous_hash)
    return sha256(canonical_json_bytes(preimage)).hexdigest()


def first_invalid_audit_sequence(
    links: Sequence[StoredAuditLink],
) -> int | None:
    """
    Return the first invalid sequence, or None for a valid chain.

    The verification starts at the first event and checks ordering,
    previous-hash linkage, and the canonical event hash.
    """
    expected_previous_hash: str | None = None
    previous_sequence = 0

    for link in links:
        sequence = link.event.sequence

        try:
            _validate_digest(link.previous_hash, "previous_hash")
            _validate_digest(link.event_hash, "event_hash")
        except ValueError:
            return sequence

        if sequence <= previous_sequence:
            return sequence

        if link.previous_hash != expected_previous_hash:
            return sequence

        expected_event_hash = audit_event_sha256(
            link.event,
            link.previous_hash,
        )

        if not compare_digest(
            link.event_hash,
            expected_event_hash,
        ):
            return sequence

        previous_sequence = sequence
        expected_previous_hash = link.event_hash

    return None
