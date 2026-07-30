from __future__ import annotations

import asyncio
import csv
import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from statistics import fmean, stdev
from time import perf_counter_ns
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from shared.schemas import (
    AccessDecision,
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    OperationId,
    RetrieveAuditRequest,
    RetrieveAuditResult,
    RetrieveRecordRequest,
    RetrieveRecordResult,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)

OperationRequest = (
    CreateRecordRequest
    | RetrieveRecordRequest
    | UpdateAuthorizationRequest
    | EvaluateAccessRequest
    | VerifyIntegrityRequest
    | RetrieveAuditRequest
)

_REQUEST_TYPES: dict[OperationId, type[BaseModel]] = {
    OperationId.CREATE_RECORD: CreateRecordRequest,
    OperationId.RETRIEVE_RECORD: RetrieveRecordRequest,
    OperationId.UPDATE_AUTHORIZATION: UpdateAuthorizationRequest,
    OperationId.EVALUATE_ACCESS: EvaluateAccessRequest,
    OperationId.VERIFY_INTEGRITY: VerifyIntegrityRequest,
    OperationId.RETRIEVE_AUDIT: RetrieveAuditRequest,
}

_RESULT_TYPES: dict[OperationId, type[BaseModel]] = {
    OperationId.CREATE_RECORD: CreateRecordResult,
    OperationId.RETRIEVE_RECORD: RetrieveRecordResult,
    OperationId.UPDATE_AUTHORIZATION: UpdateAuthorizationResult,
    OperationId.EVALUATE_ACCESS: EvaluateAccessResult,
    OperationId.VERIFY_INTEGRITY: VerifyIntegrityResult,
    OperationId.RETRIEVE_AUDIT: RetrieveAuditResult,
}

_FABRIC_WRITE_OPERATIONS = {
    OperationId.CREATE_RECORD,
    OperationId.UPDATE_AUTHORIZATION,
    OperationId.EVALUATE_ACCESS,
}


class OutcomeClassification(StrEnum):
    CORRECT = "correct_operation_outcome"
    INCORRECT = "incorrect_application_result"
    BACKEND_FAILURE = "backend_or_platform_failure"
    BENCHMARK_FAILURE = "benchmark_infrastructure_failure"


@dataclass(frozen=True, slots=True)
class PlannedRequest:
    request_id: str
    sequence: int
    operation: OperationId
    payload: OperationRequest
    expected_http_status: int
    expected_authorization_decision: AccessDecision | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")
        if self.sequence < 1:
            raise ValueError("sequence must be at least 1")
        if not 100 <= self.expected_http_status <= 599:
            raise ValueError(
                "expected_http_status must be a valid HTTP status"
            )

        expected_type = _REQUEST_TYPES.get(self.operation)
        if expected_type is None or not isinstance(
            self.payload,
            expected_type,
        ):
            raise TypeError(
                "payload type does not match the operation"
            )

        if (
            self.expected_authorization_decision is not None
            and self.operation is not OperationId.EVALUATE_ACCESS
        ):
            raise ValueError(
                "expected authorization decision is only valid for OP4"
            )


@dataclass(frozen=True, slots=True)
class RunContext:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    workload_size: int
    concurrency: int

    def __post_init__(self) -> None:
        text_fields = (
            self.batch_id,
            self.pair_id,
            self.attempt_id,
            self.architecture,
            self.run_id,
        )
        if any(not value for value in text_fields):
            raise ValueError("run identifiers must not be empty")
        if self.repetition < 1:
            raise ValueError("repetition must be at least 1")
        if self.workload_size < 1:
            raise ValueError("workload_size must be at least 1")
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.architecture not in {"traditional", "fabric"}:
            raise ValueError(
                "architecture must be traditional or fabric"
            )


@dataclass(frozen=True, slots=True)
class RequestObservation:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    request_id: str
    operation: str
    workload_size: int
    concurrency: int
    request_sequence: int
    expected_http_status: int
    expected_authorization_decision: str | None
    observed_http_status: int | None
    correctness_result: bool
    outcome_classification: str
    latency_ms: float | None
    time_to_failure_ms: float | None
    error_type: str | None
    error_message: str | None
    fabric_transaction_id: str | None
    fabric_commit_validation_status: str | None
    late_status: str | None
    started_at_utc: str


@dataclass(frozen=True, slots=True)
class RunSummary:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    operation: str
    workload_size: int
    concurrency: int
    attempted_requests: int
    response_count: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    wall_duration_seconds: float
    successful_throughput_ops_s: float
    completed_throughput_ops_s: float
    mean_latency_ms: float | None
    latency_sd_ms: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _redact_error(message: str, *, limit: int = 240) -> str:
    normalized = " ".join(message.split())
    return normalized[:limit]


def _percentile(
    values: Sequence[float],
    percentile: float,
) -> float | None:
    if not values:
        return None

    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)

    if lower == upper:
        return ordered[lower]

    fraction = rank - lower
    return (
        ordered[lower]
        + (ordered[upper] - ordered[lower]) * fraction
    )


def _validated_result(
    request: PlannedRequest,
    response: httpx.Response,
) -> BaseModel:
    result_type = _RESULT_TYPES[request.operation]
    result = result_type.model_validate(
        response.json(),
        strict=False,
    )

    expected_decision = request.expected_authorization_decision
    if expected_decision is not None:
        if not isinstance(result, EvaluateAccessResult):
            raise ValueError(
                "authorization decision expected from non-OP4 result"
            )
        if result.decision is not expected_decision:
            raise ValueError(
                f"expected decision {expected_decision.value}, "
                f"observed {result.decision.value}"
            )

    return result


def _base_observation(
    context: RunContext,
    request: PlannedRequest,
    *,
    started_at: datetime,
    observed_status: int | None,
    correct: bool,
    outcome: OutcomeClassification,
    latency_ms: float | None,
    failure_ms: float | None,
    error_type: str | None = None,
    error_message: str | None = None,
    transaction_id: str | None = None,
    validation_status: str | None = None,
) -> RequestObservation:
    return RequestObservation(
        batch_id=context.batch_id,
        pair_id=context.pair_id,
        repetition=context.repetition,
        attempt_id=context.attempt_id,
        architecture=context.architecture,
        run_id=context.run_id,
        request_id=request.request_id,
        operation=request.operation.value,
        workload_size=context.workload_size,
        concurrency=context.concurrency,
        request_sequence=request.sequence,
        expected_http_status=request.expected_http_status,
        expected_authorization_decision=(
            request.expected_authorization_decision.value
            if request.expected_authorization_decision is not None
            else None
        ),
        observed_http_status=observed_status,
        correctness_result=correct,
        outcome_classification=outcome.value,
        latency_ms=latency_ms,
        time_to_failure_ms=failure_ms,
        error_type=error_type,
        error_message=error_message,
        fabric_transaction_id=transaction_id,
        fabric_commit_validation_status=validation_status,
        late_status=None,
        started_at_utc=started_at.isoformat(),
    )


async def _execute_request(
    client: httpx.AsyncClient,
    context: RunContext,
    request: PlannedRequest,
    *,
    clock_ns: Callable[[], int],
    utc_now: Callable[[], datetime],
) -> RequestObservation:
    started_at = utc_now()
    started_ns = clock_ns()

    try:
        response = await client.post(
            f"/v1/operations/{request.operation.value}",
            json=request.payload.model_dump(mode="json"),
        )
    except httpx.RequestError as exc:
        elapsed_ms = (clock_ns() - started_ns) / 1_000_000
        return _base_observation(
            context,
            request,
            started_at=started_at,
            observed_status=None,
            correct=False,
            outcome=OutcomeClassification.BACKEND_FAILURE,
            latency_ms=None,
            failure_ms=elapsed_ms,
            error_type=type(exc).__name__,
            error_message=_redact_error(str(exc)),
        )

    elapsed_ms = (clock_ns() - started_ns) / 1_000_000
    transaction_id = response.headers.get("X-Fabric-Tx-Id")
    validation_status = response.headers.get(
        "X-Fabric-Validation-Code"
    )

    if response.status_code != request.expected_http_status:
        classification = (
            OutcomeClassification.BACKEND_FAILURE
            if response.status_code >= 500
            else OutcomeClassification.INCORRECT
        )
        return _base_observation(
            context,
            request,
            started_at=started_at,
            observed_status=response.status_code,
            correct=False,
            outcome=classification,
            latency_ms=None,
            failure_ms=elapsed_ms,
            error_type=f"HTTP_{response.status_code}",
            error_message=_redact_error(response.text),
            transaction_id=transaction_id,
            validation_status=validation_status,
        )

    if (
        context.architecture == "fabric"
        and request.operation in _FABRIC_WRITE_OPERATIONS
        and (
            transaction_id is None
            or validation_status != "VALID"
        )
    ):
        return _base_observation(
            context,
            request,
            started_at=started_at,
            observed_status=response.status_code,
            correct=False,
            outcome=(
                OutcomeClassification.BACKEND_FAILURE
                if validation_status not in {None, "VALID"}
                else OutcomeClassification.INCORRECT
            ),
            latency_ms=None,
            failure_ms=elapsed_ms,
            error_type="FabricCommitNotConfirmed",
            error_message=(
                "Fabric write response did not include a confirmed "
                "VALID commit receipt"
            ),
            transaction_id=transaction_id,
            validation_status=validation_status,
        )

    try:
        _validated_result(request, response)
    except (ValueError, ValidationError) as exc:
        return _base_observation(
            context,
            request,
            started_at=started_at,
            observed_status=response.status_code,
            correct=False,
            outcome=OutcomeClassification.INCORRECT,
            latency_ms=None,
            failure_ms=elapsed_ms,
            error_type=type(exc).__name__,
            error_message=_redact_error(str(exc)),
            transaction_id=transaction_id,
            validation_status=validation_status,
        )

    return _base_observation(
        context,
        request,
        started_at=started_at,
        observed_status=response.status_code,
        correct=True,
        outcome=OutcomeClassification.CORRECT,
        latency_ms=elapsed_ms,
        failure_ms=None,
        transaction_id=transaction_id,
        validation_status=validation_status,
    )


async def execute_http_run(
    plan: Sequence[PlannedRequest],
    context: RunContext,
    *,
    base_url: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None = None,
    clock_ns: Callable[[], int] = perf_counter_ns,
    utc_now: Callable[[], datetime] = _utc_now,
) -> tuple[tuple[RequestObservation, ...], RunSummary]:
    """Execute one measured operation run with bounded concurrency."""
    if len(plan) != context.workload_size:
        raise ValueError(
            "plan length must equal context.workload_size"
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not plan:
        raise ValueError("plan must not be empty")

    operation = plan[0].operation
    if any(item.operation is not operation for item in plan):
        raise ValueError("one run must contain exactly one operation")

    sequences = [item.sequence for item in plan]
    if len(set(sequences)) != len(sequences):
        raise ValueError("request sequences must be unique")

    queue: asyncio.Queue[PlannedRequest] = asyncio.Queue()
    for item in sorted(plan, key=lambda request: request.sequence):
        queue.put_nowait(item)

    observations: list[RequestObservation] = []

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
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    observation = await _execute_request(
                        client,
                        context,
                        item,
                        clock_ns=clock_ns,
                        utc_now=utc_now,
                    )
                    observations.append(observation)
                finally:
                    queue.task_done()

        wall_started_ns = clock_ns()
        workers = [
            asyncio.create_task(worker())
            for _ in range(
                min(context.concurrency, len(plan))
            )
        ]
        await asyncio.gather(*workers)
        wall_duration_seconds = (
            clock_ns() - wall_started_ns
        ) / 1_000_000_000

    ordered = tuple(
        sorted(
            observations,
            key=lambda observation: observation.request_sequence,
        )
    )
    summary = summarize_run(
        ordered,
        context,
        operation=operation,
        wall_duration_seconds=wall_duration_seconds,
    )
    return ordered, summary


def summarize_run(
    observations: Sequence[RequestObservation],
    context: RunContext,
    *,
    operation: OperationId,
    wall_duration_seconds: float,
) -> RunSummary:
    if wall_duration_seconds <= 0:
        raise ValueError("wall_duration_seconds must be positive")
    if len(observations) != context.workload_size:
        raise ValueError(
            "observation count must equal context.workload_size"
        )

    successful = [
        item
        for item in observations
        if item.correctness_result
        and item.outcome_classification
        == OutcomeClassification.CORRECT.value
    ]
    latencies = [
        item.latency_ms
        for item in successful
        if item.latency_ms is not None
    ]
    success_count = len(successful)
    attempted = len(observations)
    response_count = sum(
        item.observed_http_status is not None
        for item in observations
    )

    return RunSummary(
        batch_id=context.batch_id,
        pair_id=context.pair_id,
        repetition=context.repetition,
        attempt_id=context.attempt_id,
        architecture=context.architecture,
        run_id=context.run_id,
        operation=operation.value,
        workload_size=context.workload_size,
        concurrency=context.concurrency,
        attempted_requests=attempted,
        response_count=response_count,
        successful_requests=success_count,
        failed_requests=attempted - success_count,
        success_rate=success_count / attempted,
        wall_duration_seconds=wall_duration_seconds,
        successful_throughput_ops_s=(
            success_count / wall_duration_seconds
        ),
        completed_throughput_ops_s=(
            response_count / wall_duration_seconds
        ),
        mean_latency_ms=(
            fmean(latencies) if latencies else None
        ),
        latency_sd_ms=(
            stdev(latencies) if len(latencies) > 1 else None
        ),
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        p99_latency_ms=_percentile(latencies, 0.99),
    )


def _write_dataclass_rows(
    path: Path,
    rows: Sequence[Any],
    row_type: type[Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(row_type)]

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="raise",
        )
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def write_request_observations(
    path: Path,
    rows: Sequence[RequestObservation],
) -> None:
    _write_dataclass_rows(path, rows, RequestObservation)


def write_run_summaries(
    path: Path,
    rows: Sequence[RunSummary],
) -> None:
    _write_dataclass_rows(path, rows, RunSummary)
