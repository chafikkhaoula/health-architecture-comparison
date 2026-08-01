from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import httpx

from benchmark.runner import (
    RequestObservation,
    RunContext,
    RunSummary,
    execute_http_run,
    write_request_observations,
    write_run_summaries,
)
from benchmark.workload import (
    build_operation_plan,
    build_workload_resources,
)
from shared.schemas import (
    AccessDecision,
    ActorContext,
    OperationId,
)

OPERATION_ORDER = (
    OperationId.CREATE_RECORD,
    OperationId.UPDATE_AUTHORIZATION,
    OperationId.RETRIEVE_RECORD,
    OperationId.EVALUATE_ACCESS,
    OperationId.VERIFY_INTEGRITY,
    OperationId.RETRIEVE_AUDIT,
)

ADMINISTRATOR = ActorContext(
    actor_id="administrator-pilot",
    organization_id="org-pilot",
)
PRINCIPAL = ActorContext(
    actor_id="clinician-pilot",
    organization_id="org-pilot",
)


@dataclass(frozen=True, slots=True)
class PilotConfig:
    batch_id: str
    traditional_url: str
    fabric_url: str
    workload_size: int
    concurrency: int
    repetitions: int
    seed: int
    timeout_seconds: float
    output_root: Path

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValueError("batch_id must not be empty")
        if self.workload_size < 1:
            raise ValueError("workload_size must be at least 1")
        if not 1 <= self.concurrency <= self.workload_size:
            raise ValueError(
                "concurrency must be between 1 and workload_size"
            )
        if self.repetitions < 1:
            raise ValueError("repetitions must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


def architecture_order(repetition: int) -> tuple[str, str]:
    if repetition < 1:
        raise ValueError("repetition must be at least 1")
    if repetition % 2:
        return ("traditional", "fabric")
    return ("fabric", "traditional")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _namespace(
    batch_id: str,
    repetition: int,
    phase: str,
) -> str:
    digest = hashlib.sha256(
        f"{batch_id}:{repetition}:{phase}".encode("utf-8")
    ).hexdigest()[:10]
    return f"p{repetition:02d}{phase[0]}{digest}"


def _git_value(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip()


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _healthcheck(base_url: str) -> None:
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(10.0),
    ) as client:
        response = await client.get("/healthz")
        response.raise_for_status()


def _manifest(
    config: PilotConfig,
    *,
    started_at: datetime,
) -> dict[str, object]:
    protocol_path = Path("docs/experimental_protocol.md")
    dirty = _git_value("status", "--porcelain")
    return {
        "schema_version": 1,
        "pilot": True,
        "batch_id": config.batch_id,
        "status": "running",
        "started_at_utc": started_at.isoformat(),
        "ended_at_utc": None,
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": dirty is None or bool(dirty),
        "protocol_sha256": _sha256(protocol_path),
        "workload_size": config.workload_size,
        "concurrency": config.concurrency,
        "repetitions": config.repetitions,
        "seed": config.seed,
        "timeout_seconds": config.timeout_seconds,
        "operation_order": [item.value for item in OPERATION_ORDER],
        "architecture_orders": {
            str(repetition): list(architecture_order(repetition))
            for repetition in range(1, config.repetitions + 1)
        },
        "base_urls": {
            "traditional": config.traditional_url,
            "fabric": config.fabric_url,
        },
        "warmup": "one unmeasured run per operation",
        "state_strategy": "namespace_isolation_pilot_only",
        "scientific_use": "pilot observations excluded from final results",
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _persist(
    output_dir: Path,
    observations: Sequence[RequestObservation],
    summaries: Sequence[RunSummary],
    manifest: dict[str, object],
) -> None:
    write_request_observations(
        output_dir / "requests.csv",
        observations,
    )
    write_run_summaries(output_dir / "runs.csv", summaries)
    _write_json(output_dir / "manifest.json", manifest)


async def run_pilot(config: PilotConfig) -> Path:
    output_dir = config.output_root / config.batch_id
    output_dir.mkdir(parents=True, exist_ok=False)

    started_at = _utc_now()
    manifest = _manifest(config, started_at=started_at)
    observations: list[RequestObservation] = []
    summaries: list[RunSummary] = []
    _persist(output_dir, observations, summaries, manifest)

    urls = {
        "traditional": config.traditional_url,
        "fabric": config.fabric_url,
    }

    try:
        for base_url in urls.values():
            await _healthcheck(base_url)

        for repetition in range(1, config.repetitions + 1):
            pair_id = f"{config.batch_id}-r{repetition:02d}"
            resources_by_phase = {
                phase: build_workload_resources(
                    config.workload_size,
                    seed=config.seed + repetition - 1,
                    namespace=_namespace(
                        config.batch_id,
                        repetition,
                        phase,
                    ),
                )
                for phase in ("warmup", "measured")
            }

            for architecture in architecture_order(repetition):
                for phase in ("warmup", "measured"):
                    namespace = _namespace(
                        config.batch_id,
                        repetition,
                        phase,
                    )
                    for operation in OPERATION_ORDER:
                        plan = build_operation_plan(
                            operation,
                            resources_by_phase[phase],
                            request_count=config.workload_size,
                            namespace=namespace,
                            administrator=ADMINISTRATOR,
                            principal=PRINCIPAL,
                            access_decision=AccessDecision.ALLOW,
                        )
                        context = RunContext(
                            batch_id=config.batch_id,
                            pair_id=pair_id,
                            repetition=repetition,
                            attempt_id=f"{pair_id}-attempt01",
                            architecture=architecture,
                            run_id=(
                                f"{pair_id}-{architecture}-{phase}-"
                                f"{operation.value}"
                            ),
                            workload_size=config.workload_size,
                            concurrency=config.concurrency,
                        )
                        run_observations, summary = (
                            await execute_http_run(
                                plan,
                                context,
                                base_url=urls[architecture],
                                timeout_seconds=config.timeout_seconds,
                            )
                        )

                        failures = [
                            item
                            for item in run_observations
                            if not item.correctness_result
                        ]
                        if phase == "measured":
                            observations.extend(run_observations)
                            summaries.append(summary)
                            _persist(
                                output_dir,
                                observations,
                                summaries,
                                manifest,
                            )

                        print(
                            f"{phase} {architecture} "
                            f"{operation.value}: "
                            f"correct={len(run_observations) - len(failures)}/"
                            f"{len(run_observations)}"
                        )
                        if failures:
                            raise RuntimeError(
                                f"{phase} {architecture} "
                                f"{operation.value} had "
                                f"{len(failures)} correctness failures"
                            )

    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["ended_at_utc"] = _utc_now().isoformat()
        manifest["failure_type"] = type(exc).__name__
        manifest["failure_message"] = str(exc)[:240]
        _persist(output_dir, observations, summaries, manifest)
        raise

    manifest["status"] = "completed"
    manifest["ended_at_utc"] = _utc_now().isoformat()
    _persist(output_dir, observations, summaries, manifest)
    return output_dir


def _default_batch_id() -> str:
    return _utc_now().strftime("pilot-%Y%m%dT%H%M%SZ")


def _parse_args(argv: Sequence[str] | None = None) -> PilotConfig:
    parser = argparse.ArgumentParser(
        description="Run a paired dual-architecture pilot experiment."
    )
    parser.add_argument("--batch-id", default=_default_batch_id())
    parser.add_argument(
        "--traditional-url",
        default="http://127.0.0.1:18000",
    )
    parser.add_argument(
        "--fabric-url",
        default="http://127.0.0.1:18001",
    )
    parser.add_argument("--workload-size", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("results/pilot"),
    )
    arguments = parser.parse_args(argv)
    return PilotConfig(
        batch_id=arguments.batch_id,
        traditional_url=arguments.traditional_url,
        fabric_url=arguments.fabric_url,
        workload_size=arguments.workload_size,
        concurrency=arguments.concurrency,
        repetitions=arguments.repetitions,
        seed=arguments.seed,
        timeout_seconds=arguments.timeout_seconds,
        output_root=arguments.output_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = _parse_args(argv)
    output_dir = asyncio.run(run_pilot(config))
    print(f"PILOT_OUTPUT={output_dir}")
    print("PHASE4_PILOT_COMPLETED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
