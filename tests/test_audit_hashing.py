from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared.hashing.audit import (
    StoredAuditLink,
    audit_event_sha256,
    first_invalid_audit_sequence,
)
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuditEvent,
    OperationId,
    RecordLocator,
    ResourceType,
)


def _record() -> RecordLocator:
    return RecordLocator(
        resource_type=ResourceType.PATIENT,
        resource_id="patient-1",
    )


def _actor() -> ActorContext:
    return ActorContext(
        actor_id="clinician-1",
        organization_id="hospital-1",
    )


def _event(
    *,
    event_id: str,
    sequence: int,
    action: OperationId,
    timestamp: datetime,
    decision: AccessDecision | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        sequence=sequence,
        record=_record(),
        actor=_actor(),
        action=action,
        decision=decision,
        timestamp=timestamp,
    )


def _valid_chain() -> tuple[StoredAuditLink, StoredAuditLink]:
    first = _event(
        event_id="event-1",
        sequence=1,
        action=OperationId.CREATE_RECORD,
        timestamp=datetime(
            2026,
            7,
            28,
            18,
            0,
            tzinfo=timezone.utc,
        ),
    )
    first_hash = audit_event_sha256(first, None)

    second = _event(
        event_id="event-2",
        sequence=2,
        action=OperationId.EVALUATE_ACCESS,
        decision=AccessDecision.DENY,
        timestamp=first.timestamp + timedelta(seconds=1),
    )
    second_hash = audit_event_sha256(second, first_hash)

    return (
        StoredAuditLink(
            event=first,
            previous_hash=None,
            event_hash=first_hash,
        ),
        StoredAuditLink(
            event=second,
            previous_hash=first_hash,
            event_hash=second_hash,
        ),
    )


def test_audit_hash_is_deterministic() -> None:
    first = _valid_chain()[0]

    assert audit_event_sha256(
        first.event,
        first.previous_hash,
    ) == audit_event_sha256(
        first.event,
        first.previous_hash,
    )


def test_valid_audit_chain_has_no_invalid_sequence() -> None:
    assert first_invalid_audit_sequence(_valid_chain()) is None


def test_modified_event_is_detected_at_first_broken_link() -> None:
    first, second = _valid_chain()

    modified_second = _event(
        event_id=second.event.event_id,
        sequence=second.event.sequence,
        action=OperationId.EVALUATE_ACCESS,
        decision=AccessDecision.ALLOW,
        timestamp=second.event.timestamp,
    )

    tampered_chain = (
        first,
        StoredAuditLink(
            event=modified_second,
            previous_hash=second.previous_hash,
            event_hash=second.event_hash,
        ),
    )

    assert first_invalid_audit_sequence(tampered_chain) == 2


def test_broken_previous_hash_is_detected() -> None:
    first, second = _valid_chain()

    broken_chain = (
        first,
        StoredAuditLink(
            event=second.event,
            previous_hash="0" * 64,
            event_hash=second.event_hash,
        ),
    )

    assert first_invalid_audit_sequence(broken_chain) == 2


def test_reordered_chain_is_detected() -> None:
    first, second = _valid_chain()

    assert first_invalid_audit_sequence((second, first)) == 2
