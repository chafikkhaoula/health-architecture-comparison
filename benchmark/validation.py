from __future__ import annotations

import asyncio
import csv
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import ValidationError

from benchmark.runner import PlannedRequest, RunContext
from benchmark.workload import build_operation_plan
from shared.hashing.canonical import payload_sha256
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuditEvent,
    FHIRResourceEnvelope,
    OperationId,
    RetrieveAuditResult,
    RetrieveRecordResult,
    VerifyIntegrityResult,
)

VALIDATION_OPERATIONS = (
    OperationId.RETRIEVE_RECORD,
    OperationId.VERIFY_INTEGRITY,
    OperationId.RETRIEVE_AUDIT,
)


@dataclass(frozen=True, slots=True)
class CorrectnessObservation:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    workload_size: int
    concurrency: int
    validation_operation: str
    request_id: str
    request_sequence: int
    expected_postcondition: str
    observed_postcondition: str | None
    correctness_result: bool
    observed_http_status: int | None
    error_type: str | None
    error_message: str | None
    checked_at_utc: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact(value: object, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def _expected_audit_events(
    events: Sequence[AuditEvent],
    *,
    administrator: ActorContext,
    principal: ActorContext,
    access_decision: AccessDecision,
) -> bool:
    if len(events) != 3:
        return False

    expected = (
        (OperationId.CREATE_RECORD, administrator, None),
        (OperationId.UPDATE_AUTHORIZATION, administrator, None),
        (OperationId.EVALUATE_ACCESS, principal, access_decision),
    )
    return all(
        event.action is action
        and event.actor == actor
        and event.decision is decision
        for event, (action, actor, decision) in zip(
            events,
            expected,
            strict=True,
        )
    )


def _validate_response(
    operation: OperationId,
    response: httpx.Response,
    resource: FHIRResourceEnvelope,
    *,
    administrator: ActorContext,
    principal: ActorContext,
    access_decision: AccessDecision,
) -> tuple[str, bool]:
    if response.status_code != 200:
        return f"HTTP {response.status_code}", False

    body = response.json()
    if operation is OperationId.RETRIEVE_RECORD:
        result = RetrieveRecordResult.model_validate(body, strict=False)
        return "payload_identity_match", result.resource == resource

    if operation is OperationId.VERIFY_INTEGRITY:
        result = VerifyIntegrityResult.model_validate(body, strict=False)
        expected_hash = payload_sha256(resource.payload)
        correct = (
            result.record.resource_type is resource.resource_type
            and result.record.resource_id == resource.resource_id
            and result.matches
            and result.observed_hash == expected_hash
            and result.authoritative_evidence.payload_hash == expected_hash
        )
        return "payload_and_authoritative_hash_match", correct

    if operation is OperationId.RETRIEVE_AUDIT:
        result = RetrieveAuditResult.model_validate(body, strict=False)
        correct = _expected_audit_events(
            result.events,
            administrator=administrator,
            principal=principal,
            access_decision=access_decision,
        )
        return f"ordered_expected_events={len(result.events)}", correct

    raise ValueError(f"unsupported validation operation: {operation}")


def _expected_postcondition(operation: OperationId) -> str:
    return {
        OperationId.RETRIEVE_RECORD: "stored_payload_equals_input",
        OperationId.VERIFY_INTEGRITY: (
            "observed_and_authoritative_sha256_equal_input"
        ),
        OperationId.RETRIEVE_AUDIT: (
            "ordered_OP1_OP3_OP4_events_with_expected_actors_and_decision"
        ),
    }[operation]


async def validate_durable_state(
    resources: Sequence[FHIRResourceEnvelope],
    context: RunContext,
    *,
    base_url: str,
    namespace: str,
    administrator: ActorContext,
    principal: ActorContext,
    access_decision: AccessDecision,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[CorrectnessObservation, ...]:
    """Check durable postconditions outside the performance boundary."""
    if len(resources) != context.workload_size:
        raise ValueError("resource count must equal context.workload_size")

    queue: asyncio.Queue[tuple[PlannedRequest, FHIRResourceEnvelope]] = (
        asyncio.Queue()
    )
    for operation in VALIDATION_OPERATIONS:
        plan = build_operation_plan(
            operation,
            resources,
            request_count=context.workload_size,
            namespace=namespace,
            administrator=administrator,
            principal=principal,
            access_decision=access_decision,
        )
        for request, resource in zip(plan, resources, strict=True):
            queue.put_nowait((request, resource))

    observations: list[CorrectnessObservation] = []
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_seconds),
        limits=httpx.Limits(
            max_connections=context.concurrency,
            max_keepalive_connections=context.concurrency,
        ),
        transport=transport,
    ) as client:

        async def worker() -> None:
            while True:
                try:
                    request, resource = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                status: int | None = None
                observed: str | None = None
                correct = False
                error_type: str | None = None
                error_message: str | None = None
                try:
                    response = await client.post(
                        f"/v1/operations/{request.operation.value}",
                        json=request.payload.model_dump(mode="json"),
                    )
                    status = response.status_code
                    observed, correct = _validate_response(
                        request.operation,
                        response,
                        resource,
                        administrator=administrator,
                        principal=principal,
                        access_decision=access_decision,
                    )
                    if not correct:
                        error_type = "DurablePostconditionMismatch"
                except (
                    httpx.HTTPError,
                    ValueError,
                    ValidationError,
                ) as exc:
                    error_type = type(exc).__name__
                    error_message = _redact(exc)
                finally:
                    observations.append(
                        CorrectnessObservation(
                            batch_id=context.batch_id,
                            pair_id=context.pair_id,
                            repetition=context.repetition,
                            attempt_id=context.attempt_id,
                            architecture=context.architecture,
                            workload_size=context.workload_size,
                            concurrency=context.concurrency,
                            validation_operation=request.operation.value,
                            request_id=request.request_id,
                            request_sequence=request.sequence,
                            expected_postcondition=_expected_postcondition(
                                request.operation
                            ),
                            observed_postcondition=observed,
                            correctness_result=correct,
                            observed_http_status=status,
                            error_type=error_type,
                            error_message=error_message,
                            checked_at_utc=_utc_now().isoformat(),
                        )
                    )
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(
                min(context.concurrency, queue.qsize())
            )
        ]
        await asyncio.gather(*workers)

    return tuple(
        sorted(
            observations,
            key=lambda row: (
                VALIDATION_OPERATIONS.index(
                    OperationId(row.validation_operation)
                ),
                row.request_sequence,
            ),
        )
    )


def write_correctness_observations(
    path: Path,
    rows: Sequence[CorrectnessObservation],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(CorrectnessObservation)]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
