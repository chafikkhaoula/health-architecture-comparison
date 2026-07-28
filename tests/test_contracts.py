from __future__ import annotations

from datetime import datetime, timezone
from inspect import iscoroutinefunction, signature

import pytest
from pydantic import ValidationError

from shared.contracts import (
    OPERATION_METHODS,
    ArchitectureAdapter,
)
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuditEvent,
    AuthorizationRule,
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    FHIRResourceEnvelope,
    IntegrityEvidence,
    OperationId,
    RecordLocator,
    ResourceType,
    RetrieveAuditRequest,
    RetrieveAuditResult,
    RetrieveRecordRequest,
    RetrieveRecordResult,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


def make_record() -> RecordLocator:
    return RecordLocator(
        resource_type=ResourceType.PATIENT,
        resource_id="patient-000001",
    )


def make_actor() -> ActorContext:
    return ActorContext(
        actor_id="practitioner-001",
        organization_id="org-001",
    )


def make_audit_event(
    *,
    sequence: int = 1,
    record: RecordLocator | None = None,
) -> AuditEvent:
    return AuditEvent(
        event_id=f"event-{sequence:06d}",
        sequence=sequence,
        record=record or make_record(),
        actor=make_actor(),
        action=OperationId.CREATE_RECORD,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class CompleteAdapter:
    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        raise NotImplementedError

    async def retrieve_record(
        self,
        request: RetrieveRecordRequest,
    ) -> RetrieveRecordResult:
        raise NotImplementedError

    async def update_authorization(
        self,
        request: UpdateAuthorizationRequest,
    ) -> UpdateAuthorizationResult:
        raise NotImplementedError

    async def evaluate_access(
        self,
        request: EvaluateAccessRequest,
    ) -> EvaluateAccessResult:
        raise NotImplementedError

    async def verify_integrity(
        self,
        request: VerifyIntegrityRequest,
    ) -> VerifyIntegrityResult:
        raise NotImplementedError

    async def retrieve_audit(
        self,
        request: RetrieveAuditRequest,
    ) -> RetrieveAuditResult:
        raise NotImplementedError


class IncompleteAdapter:
    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        raise NotImplementedError


def test_operation_method_map_covers_six_operations() -> None:
    assert set(OPERATION_METHODS) == set(OperationId)
    assert tuple(OPERATION_METHODS.values()) == (
        "create_record",
        "retrieve_record",
        "update_authorization",
        "evaluate_access",
        "verify_integrity",
        "retrieve_audit",
    )


def test_protocol_methods_are_async_and_uniform() -> None:
    for method_name in OPERATION_METHODS.values():
        method = getattr(ArchitectureAdapter, method_name)
        assert iscoroutinefunction(method)
        assert tuple(signature(method).parameters) == (
            "self",
            "request",
        )


def test_runtime_protocol_accepts_complete_adapter() -> None:
    assert isinstance(CompleteAdapter(), ArchitectureAdapter)


def test_runtime_protocol_rejects_incomplete_adapter() -> None:
    assert not isinstance(IncompleteAdapter(), ArchitectureAdapter)


def test_expected_access_denial_is_a_valid_result() -> None:
    result = EvaluateAccessResult(
        record=make_record(),
        actor=make_actor(),
        decision=AccessDecision.DENY,
    )

    assert result.decision is AccessDecision.DENY
    assert result.matched_rule_id is None


def test_authorization_rule_supports_explicit_deny() -> None:
    rule = AuthorizationRule(
        rule_id="rule-001",
        record=make_record(),
        principal=make_actor(),
        decision=AccessDecision.DENY,
    )

    assert rule.decision is AccessDecision.DENY


def test_access_audit_event_requires_decision() -> None:
    with pytest.raises(
        ValidationError,
        match="access audit events require a decision",
    ):
        AuditEvent(
            event_id="event-001",
            sequence=1,
            record=make_record(),
            actor=make_actor(),
            action=OperationId.EVALUATE_ACCESS,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_non_access_audit_event_rejects_decision() -> None:
    with pytest.raises(
        ValidationError,
        match="non-access audit events must not carry a decision",
    ):
        AuditEvent(
            event_id="event-001",
            sequence=1,
            record=make_record(),
            actor=make_actor(),
            action=OperationId.CREATE_RECORD,
            decision=AccessDecision.ALLOW,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_audit_event_rejects_non_auditable_operation() -> None:
    with pytest.raises(
        ValidationError,
        match="action is not auditable by protocol",
    ):
        AuditEvent(
            event_id="event-001",
            sequence=1,
            record=make_record(),
            actor=make_actor(),
            action=OperationId.VERIFY_INTEGRITY,
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


def test_audit_event_requires_timezone() -> None:
    with pytest.raises(
        ValidationError,
        match="timestamp must include a UTC offset",
    ):
        AuditEvent(
            event_id="event-001",
            sequence=1,
            record=make_record(),
            actor=make_actor(),
            action=OperationId.CREATE_RECORD,
            timestamp=datetime(2026, 1, 1),
        )


def test_integrity_result_must_match_hash_comparison() -> None:
    with pytest.raises(
        ValidationError,
        match="matches must equal the hash comparison result",
    ):
        VerifyIntegrityResult(
            record=make_record(),
            authoritative_evidence=IntegrityEvidence(
                payload_hash=HASH_A,
            ),
            observed_hash=HASH_B,
            matches=True,
        )


def test_audit_result_requires_strict_order() -> None:
    with pytest.raises(
        ValidationError,
        match="strictly increasing order",
    ):
        RetrieveAuditResult(
            record=make_record(),
            events=(
                make_audit_event(sequence=2),
                make_audit_event(sequence=1),
            ),
        )


def test_audit_result_requires_matching_record() -> None:
    other_record = RecordLocator(
        resource_type=ResourceType.PATIENT,
        resource_id="patient-000002",
    )

    with pytest.raises(
        ValidationError,
        match="must target the requested record",
    ):
        RetrieveAuditResult(
            record=make_record(),
            events=(
                make_audit_event(record=other_record),
            ),
        )
