from pathlib import Path

import benchmark.pilot as pilot
from benchmark.runner import RequestObservation, RunSummary


def test_architecture_order_is_counterbalanced() -> None:
    assert pilot.architecture_order(1) == ("traditional", "fabric")
    assert pilot.architecture_order(2) == ("fabric", "traditional")


def test_pilot_config_rejects_excess_concurrency(
    tmp_path: Path,
) -> None:
    try:
        pilot.PilotConfig(
            batch_id="pilot-test",
            traditional_url="http://traditional.test",
            fabric_url="http://fabric.test",
            workload_size=1,
            concurrency=2,
            repetitions=1,
            seed=42,
            timeout_seconds=1.0,
            output_root=tmp_path,
        )
    except ValueError as exc:
        assert "concurrency" in str(exc)
    else:
        raise AssertionError("expected invalid concurrency to fail")


def test_namespace_is_short_stable_and_phase_specific() -> None:
    warmup = pilot._namespace("pilot-test", 1, "warmup")
    measured = pilot._namespace("pilot-test", 1, "measured")
    assert warmup == pilot._namespace("pilot-test", 1, "warmup")
    assert warmup != measured
    assert len(warmup) <= 20


def _observation(context, request) -> RequestObservation:
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
        observed_http_status=request.expected_http_status,
        correctness_result=True,
        outcome_classification="correct_operation_outcome",
        latency_ms=1.0,
        time_to_failure_ms=None,
        error_type=None,
        error_message=None,
        fabric_transaction_id=None,
        fabric_commit_validation_status=None,
        fabric_block_number=None,
        late_status=None,
        started_at_utc="2026-08-01T00:00:00+00:00",
    )


def _summary(context, operation) -> RunSummary:
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
        attempted_requests=context.workload_size,
        response_count=context.workload_size,
        successful_requests=context.workload_size,
        failed_requests=0,
        success_rate=1.0,
        wall_duration_seconds=1.0,
        successful_throughput_ops_s=float(context.workload_size),
        completed_throughput_ops_s=float(context.workload_size),
        mean_latency_ms=1.0,
        latency_sd_ms=None,
        p50_latency_ms=1.0,
        p95_latency_ms=1.0,
        p99_latency_ms=1.0,
    )


def test_run_pilot_excludes_warmup_from_csv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = []

    async def healthcheck(_base_url: str) -> None:
        return None

    async def execute(plan, context, **_kwargs):
        calls.append(context.run_id)
        rows = tuple(_observation(context, request) for request in plan)
        return rows, _summary(context, plan[0].operation)

    monkeypatch.setattr(pilot, "_healthcheck", healthcheck)
    monkeypatch.setattr(pilot, "execute_http_run", execute)

    config = pilot.PilotConfig(
        batch_id="pilot-test",
        traditional_url="http://traditional.test",
        fabric_url="http://fabric.test",
        workload_size=1,
        concurrency=1,
        repetitions=1,
        seed=42,
        timeout_seconds=1.0,
        output_root=tmp_path,
    )
    output_dir = __import__("asyncio").run(pilot.run_pilot(config))

    requests = (output_dir / "requests.csv").read_text(
        encoding="utf-8"
    )
    runs = (output_dir / "runs.csv").read_text(encoding="utf-8")
    manifest = (output_dir / "manifest.json").read_text(
        encoding="utf-8"
    )

    assert len(calls) == 24
    assert requests.count("\n") == 13
    assert runs.count("\n") == 13
    assert "warmup" not in runs
    assert '"status": "completed"' in manifest
