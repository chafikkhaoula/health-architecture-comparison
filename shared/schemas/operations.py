from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from shared.schemas.models import (
    FHIRId,
    FHIRResourceEnvelope,
    IntegrityEvidence,
    OperationId,
    ResourceType,
    Sha256Digest,
    StrictModel,
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    ),
]

PositiveSequence = Annotated[int, Field(ge=1)]


class AccessDecision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class RecordLocator(StrictModel):
    resource_type: ResourceType
    resource_id: FHIRId


class ActorContext(StrictModel):
    actor_id: Identifier
    organization_id: Identifier


class AuthorizationRule(StrictModel):
    rule_id: Identifier
    record: RecordLocator
    principal: ActorContext
    decision: AccessDecision


class CreateRecordRequest(StrictModel):
    resource: FHIRResourceEnvelope
    actor: ActorContext


class CreateRecordResult(StrictModel):
    record: RecordLocator
    integrity: IntegrityEvidence


class RetrieveRecordRequest(StrictModel):
    record: RecordLocator
    actor: ActorContext


class RetrieveRecordResult(StrictModel):
    resource: FHIRResourceEnvelope


class UpdateAuthorizationRequest(StrictModel):
    rule: AuthorizationRule
    actor: ActorContext


class UpdateAuthorizationResult(StrictModel):
    rule: AuthorizationRule


class EvaluateAccessRequest(StrictModel):
    record: RecordLocator
    actor: ActorContext


class EvaluateAccessResult(StrictModel):
    record: RecordLocator
    actor: ActorContext
    decision: AccessDecision
    matched_rule_id: Identifier | None = None


class VerifyIntegrityRequest(StrictModel):
    record: RecordLocator


class VerifyIntegrityResult(StrictModel):
    record: RecordLocator
    authoritative_evidence: IntegrityEvidence
    observed_hash: Sha256Digest
    matches: bool

    @model_validator(mode="after")
    def validate_match_flag(self) -> Self:
        expected = (
            self.observed_hash
            == self.authoritative_evidence.payload_hash
        )
        if self.matches != expected:
            raise ValueError(
                "matches must equal the hash comparison result"
            )
        return self


class AuditEvent(StrictModel):
    event_id: Identifier
    sequence: PositiveSequence
    record: RecordLocator
    actor: ActorContext
    action: OperationId
    decision: AccessDecision | None = None
    timestamp: datetime

    @model_validator(mode="after")
    def validate_audit_semantics(self) -> Self:
        auditable_actions = {
            OperationId.CREATE_RECORD,
            OperationId.UPDATE_AUTHORIZATION,
            OperationId.EVALUATE_ACCESS,
        }
        if self.action not in auditable_actions:
            raise ValueError("action is not auditable by protocol")

        if self.action == OperationId.EVALUATE_ACCESS:
            if self.decision is None:
                raise ValueError(
                    "access audit events require a decision"
                )
        elif self.decision is not None:
            raise ValueError(
                "non-access audit events must not carry a decision"
            )

        if (
            self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
        ):
            raise ValueError("timestamp must include a UTC offset")

        return self


class RetrieveAuditRequest(StrictModel):
    record: RecordLocator


class RetrieveAuditResult(StrictModel):
    record: RecordLocator
    events: tuple[AuditEvent, ...]

    @model_validator(mode="after")
    def validate_ordered_history(self) -> Self:
        sequences = [event.sequence for event in self.events]
        if any(
            current >= following
            for current, following in zip(
                sequences,
                sequences[1:],
                strict=False,
            )
        ):
            raise ValueError(
                "audit events must be in strictly increasing order"
            )

        if any(event.record != self.record for event in self.events):
            raise ValueError(
                "audit events must target the requested record"
            )

        return self
