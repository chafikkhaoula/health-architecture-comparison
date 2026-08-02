from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import httpx

from benchmark.runner import (
    OutcomeClassification,
    RunContext,
    execute_http_run,
    write_request_observations,
    write_run_summaries,
)
from benchmark.workload import (
    build_operation_plan,
    build_workload_resources,
)
from shared.schemas import ActorContext, OperationId

ADMINISTRATOR = ActorContext(
    actor_id="administrator-runner",
    organization_id="org-runner",
)
PRINCIPAL = ActorContext(
    actor_id="clinician-runner",
    organization_id="org-runner",
)


def _context(
    *,
    workload_size: int = 4,
    concurrency: int = 2,
) -> RunContext:
    return RunContext(
        batch_id="batch-001",
        pair_id="pair-001",
        repetition=1,
        attempt_id="attempt-001",
        architecture="traditional",
        run_id="run-001",
        workload_size=workload_size,
        concurrency=concurrency,
    )


def _create_plan(count: int = 4):
    resources = build_workload_resources(
        count,
        seed=42,
        namespace="pair01",
    )
    return build_operation_plan(
        OperationId.CREATE_RECORD,
        resources,
        request_count=count,
        namespace="pair01",
        administrator=ADMINISTRATOR,
        principal=PRINCIPAL,
    )


def test_runner_bounds_concurrency_and_summarizes() -> None:
    async def scenario():
        active = 0
        peak = 0

        async def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

            payload = request.read()
            assert payload
            request_json = __import__("json").loads(payload)
            resource = request_json["resource"]

            return httpx.Response(
                201,
                json={
                    "record": {
                        "resource_type": resource["resource_type"],
                        "resource_id": resource["resource_id"],
                    },
                    "integrity": {
                        "algorithm": "sha256",
                        "canonicalization": "RFC8785",
                        "payload_hash": "0" * 64,
                    },
                },
                headers={
                    "X-Fabric-Tx-Id": "tx-test",
                    "X-Fabric-Validation-Code": "VALID",
                    "X-Fabric-Block-Number": "9",
                },
            )

        observations, summary = await execute_http_run(
            _create_plan(),
            _context(),
            base_url="http://benchmark.test",
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )
        return peak, observations, summary

    peak, observations, summary = asyncio.run(scenario())

    assert peak == 2
    assert [item.request_sequence for item in observations] == [
        1,
        2,
        3,
        4,
    ]
    assert all(item.correctness_result for item in observations)
    assert all(
        item.fabric_transaction_id == "tx-test"
        for item in observations
    )
    assert all(item.fabric_block_number == 9 for item in observations)
    assert summary.successful_requests == 4
    assert summary.failed_requests == 0
    assert summary.success_rate == 1.0
    assert summary.p50_latency_ms is not None
    assert summary.p95_latency_ms is not None
    assert summary.successful_throughput_ops_s > 0


def test_runner_keeps_incorrect_and_backend_failures() -> None:
    async def scenario():
        calls = 0

        def handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    503,
                    text="backend unavailable",
                )
            return httpx.Response(
                201,
                json={"unexpected": True},
            )

        return await execute_http_run(
            _create_plan(2),
            _context(workload_size=2, concurrency=1),
            base_url="http://benchmark.test",
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )

    observations, summary = asyncio.run(scenario())

    assert observations[0].outcome_classification == (
        OutcomeClassification.BACKEND_FAILURE.value
    )
    assert observations[1].outcome_classification == (
        OutcomeClassification.INCORRECT.value
    )
    assert all(
        item.latency_ms is None for item in observations
    )
    assert all(
        item.time_to_failure_ms is not None
        for item in observations
    )
    assert summary.successful_requests == 0
    assert summary.failed_requests == 2
    assert summary.mean_latency_ms is None
    assert summary.p95_latency_ms is None


def test_runner_records_transport_failure_without_retry() -> None:
    async def scenario():
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout(
                "timed out",
                request=request,
            )

        observations, summary = await execute_http_run(
            _create_plan(1),
            _context(workload_size=1, concurrency=1),
            base_url="http://benchmark.test",
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )
        return calls, observations, summary

    calls, observations, summary = asyncio.run(scenario())

    assert calls == 1
    assert observations[0].observed_http_status is None
    assert observations[0].error_type == "ReadTimeout"
    assert observations[0].time_to_failure_ms is not None
    assert summary.response_count == 0
    assert summary.failed_requests == 1


def test_fabric_write_requires_valid_commit_receipt() -> None:
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            request_json = __import__("json").loads(
                request.read()
            )
            resource = request_json["resource"]
            return httpx.Response(
                201,
                json={
                    "record": {
                        "resource_type": resource[
                            "resource_type"
                        ],
                        "resource_id": resource["resource_id"],
                    },
                    "integrity": {
                        "payload_hash": "0" * 64,
                    },
                },
            )

        context = RunContext(
            batch_id="batch-001",
            pair_id="pair-001",
            repetition=1,
            attempt_id="attempt-001",
            architecture="fabric",
            run_id="run-fabric-001",
            workload_size=1,
            concurrency=1,
        )
        return await execute_http_run(
            _create_plan(1),
            context,
            base_url="http://benchmark.test",
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )

    observations, summary = asyncio.run(scenario())

    assert observations[0].correctness_result is False
    assert observations[0].error_type == (
        "FabricCommitNotConfirmed"
    )
    assert observations[0].outcome_classification == (
        OutcomeClassification.INCORRECT.value
    )
    assert summary.successful_requests == 0


def test_csv_writers_preserve_schema(
    tmp_path: Path,
) -> None:
    async def scenario():
        def handler(
            request: httpx.Request,
        ) -> httpx.Response:
            request_json = __import__("json").loads(
                request.read()
            )
            resource = request_json["resource"]
            return httpx.Response(
                201,
                json={
                    "record": {
                        "resource_type": resource[
                            "resource_type"
                        ],
                        "resource_id": resource["resource_id"],
                    },
                    "integrity": {
                        "payload_hash": "0" * 64,
                    },
                },
            )

        return await execute_http_run(
            _create_plan(1),
            _context(workload_size=1, concurrency=1),
            base_url="http://benchmark.test",
            timeout_seconds=1.0,
            transport=httpx.MockTransport(handler),
        )

    observations, summary = asyncio.run(scenario())

    requests_path = tmp_path / "requests.csv"
    runs_path = tmp_path / "runs.csv"
    write_request_observations(requests_path, observations)
    write_run_summaries(runs_path, [summary])

    with requests_path.open(newline="", encoding="utf-8") as handle:
        request_rows = list(csv.DictReader(handle))
    with runs_path.open(newline="", encoding="utf-8") as handle:
        run_rows = list(csv.DictReader(handle))

    assert request_rows[0]["request_id"] == "pair01-OP1-000001"
    assert request_rows[0]["latency_ms"]
    assert run_rows[0]["successful_requests"] == "1"
    assert run_rows[0]["p95_latency_ms"]
