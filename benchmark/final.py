from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import httpx

from benchmark.resources import (
    DockerStatsMonitor,
    ResourceContext,
    ResourceSample,
    capture_storage,
    docker_container_ids,
    managed_api_pid,
    write_resource_samples,
    write_storage_observations,
)
from benchmark.runner import (
    RequestObservation,
    RunContext,
    RunSummary,
    execute_http_run,
    write_request_observations,
    write_run_summaries,
)
from benchmark.validation import (
    validate_durable_state,
    write_correctness_observations,
)
from benchmark.workload import (
    build_operation_plan,
    build_workload_resources,
)
from shared.hashing.canonical import canonical_json_bytes
from shared.schemas import AccessDecision, ActorContext, OperationId

OPERATION_ORDER = (
    OperationId.CREATE_RECORD,
    OperationId.UPDATE_AUTHORIZATION,
    OperationId.RETRIEVE_RECORD,
    OperationId.EVALUATE_ACCESS,
    OperationId.VERIFY_INTEGRITY,
    OperationId.RETRIEVE_AUDIT,
)
ADMINISTRATOR = ActorContext(
    actor_id="administrator-final",
    organization_id="org-final",
)
PRINCIPAL = ActorContext(
    actor_id="clinician-final",
    organization_id="org-final",
)


class BenchmarkInfrastructureError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FinalConfig:
    batch_id: str
    traditional_url: str
    fabric_url: str
    workload_sizes: tuple[int, ...]
    concurrencies: tuple[int, ...]
    repetitions: int
    seed: int
    timeout_seconds: float
    resource_sampling_interval_seconds: float
    output_root: Path
    driver: Path
    resume: bool = False
    smoke: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.batch_id):
            raise ValueError("batch_id contains unsupported characters")
        if not self.workload_sizes or any(
            type(value) is not int or value < 1
            for value in self.workload_sizes
        ):
            raise ValueError("workload sizes must be positive integers")
        if not self.concurrencies or any(
            type(value) is not int or value < 1
            for value in self.concurrencies
        ):
            raise ValueError("concurrencies must be positive integers")
        if max(self.concurrencies) > min(self.workload_sizes):
            raise ValueError("concurrency cannot exceed a workload size")
        if self.repetitions < 1:
            raise ValueError("repetitions must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if self.resource_sampling_interval_seconds <= 0:
            raise ValueError("resource sampling interval must be positive")


@dataclass(frozen=True, slots=True)
class BlockBoundary:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    operation: str
    workload_size: int
    concurrency: int
    height_before: int | None
    height_after: int | None
    minimum_observed_block: int | None
    maximum_observed_block: int | None
    observed_valid_transactions: int
    observed_distinct_blocks: int


@dataclass(frozen=True, slots=True)
class InputDataset:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    workload_size: int
    concurrency: int
    seed: int
    namespace: str
    schema_version: int
    record_count: int
    dataset_sha256: str


@dataclass(frozen=True, slots=True)
class Exclusion:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    workload_size: int
    concurrency: int
    reason_code: str
    reason_message: str
    invalidated_at_utc: str


@dataclass(frozen=True, slots=True)
class FabricBlock:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    run_id: str
    operation: str
    workload_size: int
    concurrency: int
    block_number: int
    observed_valid_transactions: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def architecture_order(repetition: int) -> tuple[str, str]:
    if repetition < 1:
        raise ValueError("repetition must be positive")
    return (
        ("traditional", "fabric")
        if repetition % 2
        else ("fabric", "traditional")
    )


def _namespace(
    batch_id: str,
    workload_size: int,
    concurrency: int,
    repetition: int,
    attempt: int,
    phase: str,
) -> str:
    digest = hashlib.sha256(
        (
            f"{batch_id}:{workload_size}:{concurrency}:"
            f"{repetition}:{attempt}:{phase}"
        ).encode()
    ).hexdigest()[:12]
    return f"f{phase[0]}{digest}"


def _pair_key(
    workload_size: int,
    concurrency: int,
    repetition: int,
) -> str:
    return f"n{workload_size:04d}-c{concurrency:02d}-r{repetition:02d}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _command_json(*arguments: str) -> object:
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _allowlisted_environment(path: Path, names: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() in names:
            values[name.strip()] = value.strip()
    return values


def _host_metadata() -> dict[str, object]:
    memory: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, _, raw_value = line.partition(":")
        if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[name] = int(raw_value.split()[0]) * 1024
    cpu_model = None
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("model name"):
            cpu_model = line.partition(":")[2].strip()
            break
    return {
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "cpu_model": cpu_model,
        "logical_cpu_count": os.cpu_count(),
        "memory": memory,
    }


def _source_hashes() -> dict[str, str]:
    return {
        name: _sha256(Path(name))
        for name in _git("ls-files").splitlines()
        if Path(name).is_file()
    }


def _docker_metadata() -> dict[str, object]:
    images = sorted(
        set(
            subprocess.run(
                (
                    "docker",
                    "compose",
                    "-f",
                    "architecture-traditional/compose.yaml",
                    "config",
                    "--images",
                ),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            + subprocess.run(
                (
                    "docker",
                    "compose",
                    "-f",
                    "architecture-fabric/compose.yaml",
                    "config",
                    "--images",
                ),
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "POSTGRES_DB": "redacted",
                    "POSTGRES_ADMIN_USER": "redacted",
                    "POSTGRES_ADMIN_PASSWORD": "redacted",
                    "APP_DB_USER": "redacted",
                    "APP_DB_PASSWORD": "redacted",
                },
            ).stdout.splitlines()
        )
    )
    inspected = _command_json("docker", "image", "inspect", *images)
    return {
        "engine": _command_json(
            "docker", "version", "--format", "{{json .}}"
        ),
        "compose_version": subprocess.run(
            ("docker", "compose", "version", "--short"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "images": [
            {
                "id": item.get("Id"),
                "repo_digests": item.get("RepoDigests", []),
                "repo_tags": item.get("RepoTags", []),
            }
            for item in inspected
        ],
        "background_containers_at_start": subprocess.run(
            (
                "docker",
                "ps",
                "--format",
                "{{.ID}} {{.Image}} {{.Names}} {{.Status}}",
            ),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines(),
    }


def _runtime_container_images() -> list[dict[str, object]]:
    identifiers: set[str] = set()
    for filter_value in (
        "label=com.docker.compose.project=health-arch-traditional",
        "label=com.docker.compose.project=health-arch-fabric",
        "network=health-arch-fabric-net",
    ):
        completed = subprocess.run(
            (
                "docker",
                "ps",
                "--all",
                "--filter",
                filter_value,
                "--format",
                "{{.ID}}",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        identifiers.update(completed.stdout.split())
    if not identifiers:
        return []
    inspected = _command_json(
        "docker", "container", "inspect", *sorted(identifiers)
    )
    return [
        {
            "container_id": item.get("Id"),
            "container_name": str(item.get("Name", "")).removeprefix("/"),
            "image_id": item.get("Image"),
            "configured_image": item.get("Config", {}).get("Image"),
        }
        for item in inspected
    ]


class EnvironmentDriver:
    def __init__(self, path: Path, batch_dir: Path) -> None:
        self.path = path.resolve()
        self.batch_dir = batch_dir

    def run(
        self,
        action: str,
        architecture: str | None = None,
        *,
        log_path: Path | None = None,
    ) -> str:
        command = [str(self.path), action]
        if architecture is not None:
            command.append(architecture)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        output = completed.stdout + completed.stderr
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[{_utc_now().isoformat()}] {' '.join(command)}\n"
                )
                handle.write(output)
        if completed.returncode != 0:
            raise BenchmarkInfrastructureError(
                f"environment action failed: {action} {architecture or ''}"
            )
        return completed.stdout.strip()

    def height(self, *, log_path: Path) -> int:
        return int(self.run("height", "fabric", log_path=log_path))


def _prime_if_fabric(
    driver: EnvironmentDriver,
    architecture: str,
    *,
    log_path: Path,
) -> None:
    if architecture == "fabric":
        driver.run("prime", architecture, log_path=log_path)


class BatchCheckpoint:
    def __init__(self, config: FinalConfig) -> None:
        self.config = config
        self.output_dir = config.output_root / config.batch_id
        self.manifest_path = self.output_dir / "manifest.json"
        self.progress_path = self.output_dir / "progress.json"
        if config.resume:
            if not self.output_dir.is_dir():
                raise FileNotFoundError(
                    f"cannot resume missing batch {self.output_dir}"
                )
            self.manifest = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
            self.progress = json.loads(
                self.progress_path.read_text(encoding="utf-8")
            )
            self._validate_resume()
            self._recover_interrupted()
        else:
            self.output_dir.mkdir(parents=True, exist_ok=False)
            self.manifest = self._new_manifest()
            self.progress = {
                "schema_version": 1,
                "status": "running",
                "attempts": {},
                "completed_pairs": {},
                "updated_at_utc": _utc_now().isoformat(),
            }
            _atomic_json(
                self.output_dir / "source_hashes.json",
                _source_hashes(),
            )
            self.persist()

    def _new_manifest(self) -> dict[str, object]:
        dirty = _git("status", "--porcelain")
        if dirty:
            raise RuntimeError("final experiments require a clean worktree")
        commit = _git("rev-parse", "HEAD")
        protocol = Path("docs/experimental_protocol.md")
        allowlist = {
            "FABRIC_VERSION",
            "CHANNEL_NAME",
            "CHAINCODE_NAME",
            "CHAINCODE_VERSION",
            "CHAINCODE_SEQUENCE",
            "ORDERER_PORT",
            "ORG1_PEER_PORT",
            "ORG2_PEER_PORT",
            "GATEWAY_BRIDGE_PORT",
            "POSTGRES_DB",
            "POSTGRES_PORT",
            "DB_POOL_MIN_SIZE",
            "DB_POOL_MAX_SIZE",
        }
        return {
            "schema_version": 1,
            "pilot": self.config.smoke,
            "scientific_use": (
                "orchestration smoke excluded from final results"
                if self.config.smoke
                else "locked final performance experiment"
            ),
            "batch_id": self.config.batch_id,
            "status": "running",
            "started_at_utc": _utc_now().isoformat(),
            "ended_at_utc": None,
            "git_commit": commit,
            "git_dirty": False,
            "protocol_sha256": _sha256(protocol),
            "source_hashes_file": "source_hashes.json",
            "input_dataset_index": {
                "path": "inputs.csv",
                "schema_version": 1,
                "generator": "shared.data_generator.generate_fhir_resources",
            },
            "dependencies_sha256": {
                name: _sha256(Path(name))
                for name in ("requirements.txt", "requirements-dev.txt")
            },
            "workload_matrix": {
                "workload_sizes": list(self.config.workload_sizes),
                "concurrencies": list(self.config.concurrencies),
                "repetitions": self.config.repetitions,
            },
            "seed": self.config.seed,
            "timeout_seconds": self.config.timeout_seconds,
            "resource_sampling_interval_seconds": (
                self.config.resource_sampling_interval_seconds
            ),
            "operation_order": [item.value for item in OPERATION_ORDER],
            "warmup": "one unmeasured run per operation followed by full reset",
            "attempt_policy": "atomic_traditional_fabric_pair",
            "architecture_orders": {
                str(repetition): list(architecture_order(repetition))
                for repetition in range(1, self.config.repetitions + 1)
            },
            "base_urls": {
                "traditional": self.config.traditional_url,
                "fabric": self.config.fabric_url,
            },
            "host": _host_metadata(),
            "docker": _docker_metadata(),
            "configuration": {
                "traditional": _allowlisted_environment(
                    Path("architecture-traditional/.env"), allowlist
                ),
                "fabric": _allowlisted_environment(
                    Path("architecture-fabric/.env"), allowlist
                ),
            },
            "fabric_block_metadata": (
                "response_receipt_block_number_and_channel_height_boundaries"
            ),
        }

    def _validate_resume(self) -> None:
        expected = {
            "workload_sizes": list(self.config.workload_sizes),
            "concurrencies": list(self.config.concurrencies),
            "repetitions": self.config.repetitions,
        }
        if self.manifest.get("workload_matrix") != expected:
            raise ValueError("resume workload matrix differs from manifest")
        if self.manifest.get("git_commit") != _git("rev-parse", "HEAD"):
            raise ValueError("resume commit differs from manifest")
        if _git("status", "--porcelain"):
            raise ValueError("resume requires a clean worktree")
        if self.manifest.get("status") == "completed":
            raise ValueError("batch is already completed")

    def _recover_interrupted(self) -> None:
        changed = False
        for pair_key, attempts in self.progress["attempts"].items():
            for attempt in attempts:
                if attempt["status"] == "running":
                    attempt.update(
                        status="invalid",
                        reason_code="interrupted_attempt",
                        reason_message=(
                            "process ended before atomic pair completion"
                        ),
                        ended_at_utc=_utc_now().isoformat(),
                    )
                    self.progress["completed_pairs"].pop(pair_key, None)
                    changed = True
        if changed:
            self.persist()

    def persist(self) -> None:
        self.progress["updated_at_utc"] = _utc_now().isoformat()
        _atomic_json(self.manifest_path, self.manifest)
        _atomic_json(self.progress_path, self.progress)

    def is_complete(self, pair_key: str) -> bool:
        return pair_key in self.progress["completed_pairs"]

    def begin_attempt(
        self,
        pair_key: str,
        *,
        workload_size: int,
        concurrency: int,
        repetition: int,
    ) -> tuple[int, str, Path]:
        attempts = self.progress["attempts"].setdefault(pair_key, [])
        attempt_number = len(attempts) + 1
        attempt_id = f"{pair_key}-attempt{attempt_number:02d}"
        attempts.append(
            {
                "attempt_number": attempt_number,
                "attempt_id": attempt_id,
                "status": "running",
                "workload_size": workload_size,
                "concurrency": concurrency,
                "repetition": repetition,
                "phase": "created",
                "started_at_utc": _utc_now().isoformat(),
                "ended_at_utc": None,
            }
        )
        attempt_dir = self.output_dir / "attempts" / pair_key / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=False)
        self.persist()
        return attempt_number, attempt_id, attempt_dir

    def update_phase(self, pair_key: str, phase: str) -> None:
        self.progress["attempts"][pair_key][-1]["phase"] = phase
        self.persist()

    def complete_attempt(self, pair_key: str) -> None:
        attempt = self.progress["attempts"][pair_key][-1]
        attempt.update(
            status="completed",
            phase="completed",
            ended_at_utc=_utc_now().isoformat(),
        )
        self.progress["completed_pairs"][pair_key] = attempt["attempt_id"]
        self.persist()

    def invalidate_attempt(
        self,
        pair_key: str,
        *,
        reason_code: str,
        reason_message: str,
    ) -> None:
        attempt = self.progress["attempts"][pair_key][-1]
        attempt.update(
            status="invalid",
            reason_code=reason_code,
            reason_message=" ".join(reason_message.split())[:240],
            ended_at_utc=_utc_now().isoformat(),
        )
        self.progress["completed_pairs"].pop(pair_key, None)
        self.manifest["status"] = "paused_infrastructure_failure"
        self.persist()


def _dataset_hash(resources: Sequence[object]) -> str:
    digest = hashlib.sha256()
    for resource in resources:
        digest.update(
            canonical_json_bytes(resource.model_dump(mode="json"))  # type: ignore[attr-defined]
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _write_dataclass_rows(
    path: Path,
    rows: Sequence[object],
    row_type: type,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(row_type)]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


async def _healthcheck(base_url: str) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{base_url}/healthz")
        response.raise_for_status()


async def _warmup(
    config: FinalConfig,
    *,
    architecture: str,
    pair_id: str,
    attempt_id: str,
    repetition: int,
    concurrency: int,
    namespace: str,
    resources: Sequence,
) -> None:
    base_url = (
        config.traditional_url
        if architecture == "traditional"
        else config.fabric_url
    )
    await _healthcheck(base_url)
    for operation in OPERATION_ORDER:
        plan = build_operation_plan(
            operation,
            resources,
            request_count=len(resources),
            namespace=namespace,
            administrator=ADMINISTRATOR,
            principal=PRINCIPAL,
            access_decision=AccessDecision.ALLOW,
        )
        context = RunContext(
            batch_id=config.batch_id,
            pair_id=pair_id,
            repetition=repetition,
            attempt_id=attempt_id,
            architecture=architecture,
            run_id=f"{attempt_id}-{architecture}-warmup-{operation.value}",
            workload_size=len(resources),
            concurrency=concurrency,
        )
        observations, _ = await execute_http_run(
            plan,
            context,
            base_url=base_url,
            timeout_seconds=config.timeout_seconds,
        )
        failures = sum(not item.correctness_result for item in observations)
        print(
            f"warmup {architecture} {operation.value}: "
            f"correct={len(observations) - failures}/{len(observations)}",
            flush=True,
        )
        if failures:
            raise BenchmarkInfrastructureError(
                f"warmup readiness gate failed for {architecture} {operation.value}"
            )


async def _measure_architecture(
    config: FinalConfig,
    driver: EnvironmentDriver,
    *,
    architecture: str,
    pair_id: str,
    attempt_id: str,
    repetition: int,
    workload_size: int,
    concurrency: int,
    namespace: str,
    resources: Sequence,
    output_dir: Path,
    environment_log: Path,
) -> None:
    base_url = (
        config.traditional_url
        if architecture == "traditional"
        else config.fabric_url
    )
    await _healthcheck(base_url)
    all_observations: list[RequestObservation] = []
    all_summaries: list[RunSummary] = []
    all_resources: list[ResourceSample] = []
    boundaries: list[BlockBoundary] = []

    for operation in OPERATION_ORDER:
        run_id = f"{attempt_id}-{architecture}-measured-{operation.value}"
        plan = build_operation_plan(
            operation,
            resources,
            request_count=workload_size,
            namespace=namespace,
            administrator=ADMINISTRATOR,
            principal=PRINCIPAL,
            access_decision=AccessDecision.ALLOW,
        )
        context = RunContext(
            batch_id=config.batch_id,
            pair_id=pair_id,
            repetition=repetition,
            attempt_id=attempt_id,
            architecture=architecture,
            run_id=run_id,
            workload_size=workload_size,
            concurrency=concurrency,
        )
        resource_context = ResourceContext(
            batch_id=config.batch_id,
            pair_id=pair_id,
            repetition=repetition,
            attempt_id=attempt_id,
            architecture=architecture,
            run_id=run_id,
            operation=operation.value,
            workload_size=workload_size,
            concurrency=concurrency,
        )
        height_before = (
            driver.height(log_path=environment_log)
            if architecture == "fabric"
            else None
        )
        container_ids = docker_container_ids(architecture)
        async with DockerStatsMonitor(
            resource_context,
            container_ids,
            sample_interval_seconds=(
                config.resource_sampling_interval_seconds
            ),
            process_ids={
                f"{architecture}-api": managed_api_pid(architecture)
            },
        ) as monitor:
            monitor.mark_measurement_started()
            observations, summary = await execute_http_run(
                plan,
                context,
                base_url=base_url,
                timeout_seconds=config.timeout_seconds,
            )
            await monitor.wait_for_measured_sample()
        height_after = (
            driver.height(log_path=environment_log)
            if architecture == "fabric"
            else None
        )
        all_observations.extend(observations)
        all_summaries.append(summary)
        all_resources.extend(monitor.samples)
        blocks = [
            item.fabric_block_number
            for item in observations
            if item.correctness_result
            and item.fabric_block_number is not None
        ]
        boundaries.append(
            BlockBoundary(
                batch_id=config.batch_id,
                pair_id=pair_id,
                repetition=repetition,
                attempt_id=attempt_id,
                architecture=architecture,
                run_id=run_id,
                operation=operation.value,
                workload_size=workload_size,
                concurrency=concurrency,
                height_before=height_before,
                height_after=height_after,
                minimum_observed_block=min(blocks) if blocks else None,
                maximum_observed_block=max(blocks) if blocks else None,
                observed_valid_transactions=len(blocks),
                observed_distinct_blocks=len(set(blocks)),
            )
        )
        write_request_observations(
            output_dir / "requests.csv", all_observations
        )
        write_run_summaries(output_dir / "runs.csv", all_summaries)
        write_resource_samples(output_dir / "resources.csv", all_resources)
        _write_dataclass_rows(
            output_dir / "block_boundaries.csv",
            boundaries,
            BlockBoundary,
        )
        print(
            f"measured {architecture} {operation.value}: "
            f"correct={summary.successful_requests}/"
            f"{summary.attempted_requests}",
            flush=True,
        )

    validation_context = RunContext(
        batch_id=config.batch_id,
        pair_id=pair_id,
        repetition=repetition,
        attempt_id=attempt_id,
        architecture=architecture,
        run_id=f"{attempt_id}-{architecture}-validation",
        workload_size=workload_size,
        concurrency=concurrency,
    )
    correctness = await validate_durable_state(
        resources,
        validation_context,
        base_url=base_url,
        namespace=namespace,
        administrator=ADMINISTRATOR,
        principal=PRINCIPAL,
        access_decision=AccessDecision.ALLOW,
        timeout_seconds=config.timeout_seconds,
    )
    write_correctness_observations(
        output_dir / "correctness.csv", correctness
    )
    storage_context = ResourceContext(
        batch_id=config.batch_id,
        pair_id=pair_id,
        repetition=repetition,
        attempt_id=attempt_id,
        architecture=architecture,
        run_id=f"{attempt_id}-{architecture}-storage",
        operation="storage",
        workload_size=workload_size,
        concurrency=concurrency,
    )
    storage = capture_storage(storage_context)
    write_storage_observations(output_dir / "storage.csv", storage)


async def _run_pair(
    config: FinalConfig,
    checkpoint: BatchCheckpoint,
    driver: EnvironmentDriver,
    *,
    workload_size: int,
    concurrency: int,
    repetition: int,
) -> None:
    pair_key = _pair_key(workload_size, concurrency, repetition)
    attempt_number, attempt_id, attempt_dir = checkpoint.begin_attempt(
        pair_key,
        workload_size=workload_size,
        concurrency=concurrency,
        repetition=repetition,
    )
    pair_id = f"{config.batch_id}-{pair_key}"
    warmup_namespace = _namespace(
        config.batch_id,
        workload_size,
        concurrency,
        repetition,
        attempt_number,
        "warmup",
    )
    measured_namespace = _namespace(
        config.batch_id,
        workload_size,
        concurrency,
        repetition,
        attempt_number,
        "measured",
    )
    planned_seed = config.seed + repetition - 1
    warmup_resources = build_workload_resources(
        workload_size,
        seed=planned_seed,
        namespace=warmup_namespace,
    )
    measured_resources = build_workload_resources(
        workload_size,
        seed=planned_seed,
        namespace=measured_namespace,
    )
    input_dataset = InputDataset(
        batch_id=config.batch_id,
        pair_id=pair_id,
        repetition=repetition,
        attempt_id=attempt_id,
        workload_size=workload_size,
        concurrency=concurrency,
        seed=planned_seed,
        namespace=measured_namespace,
        schema_version=1,
        record_count=len(measured_resources),
        dataset_sha256=_dataset_hash(measured_resources),
    )
    _atomic_json(attempt_dir / "input.json", asdict(input_dataset))

    try:
        for architecture in architecture_order(repetition):
            architecture_dir = attempt_dir / architecture
            architecture_dir.mkdir(parents=True, exist_ok=False)
            environment_log = architecture_dir / "environment.log"

            checkpoint.update_phase(
                pair_key, f"{architecture}_warmup_reset"
            )
            driver.run(
                "fresh-start",
                architecture,
                log_path=environment_log,
            )
            if architecture == "fabric":
                checkpoint.update_phase(
                    pair_key, "fabric_warmup_readiness"
                )
            _prime_if_fabric(
                driver,
                architecture,
                log_path=environment_log,
            )
            await _warmup(
                config,
                architecture=architecture,
                pair_id=pair_id,
                attempt_id=attempt_id,
                repetition=repetition,
                concurrency=concurrency,
                namespace=warmup_namespace,
                resources=warmup_resources,
            )

            checkpoint.update_phase(
                pair_key, f"{architecture}_measured_reset"
            )
            driver.run(
                "fresh-start",
                architecture,
                log_path=environment_log,
            )
            if architecture == "fabric":
                checkpoint.update_phase(
                    pair_key, "fabric_measured_readiness"
                )
            _prime_if_fabric(
                driver,
                architecture,
                log_path=environment_log,
            )
            if architecture == "fabric":
                live_channel_config = json.loads(
                    driver.run(
                        "channel-config",
                        architecture,
                        log_path=environment_log,
                    )
                )
                _atomic_json(
                    architecture_dir / "fabric_channel_config.json",
                    live_channel_config,
                )
            checkpoint.update_phase(
                pair_key, f"{architecture}_measured"
            )
            await _measure_architecture(
                config,
                driver,
                architecture=architecture,
                pair_id=pair_id,
                attempt_id=attempt_id,
                repetition=repetition,
                workload_size=workload_size,
                concurrency=concurrency,
                namespace=measured_namespace,
                resources=measured_resources,
                output_dir=architecture_dir,
                environment_log=environment_log,
            )
            _atomic_json(
                architecture_dir / "runtime_container_images.json",
                _runtime_container_images(),
            )
            environment_log.write_text(
                environment_log.read_text(encoding="utf-8")
                + driver.run("logs", architecture),
                encoding="utf-8",
            )
            driver.run(
                "teardown", architecture, log_path=environment_log
            )

        checkpoint.complete_attempt(pair_key)
    except BaseException as exc:
        for architecture in ("traditional", "fabric"):
            try:
                log_dir = attempt_dir / architecture
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "failure_environment.log").write_text(
                    driver.run("logs", architecture) + "\n",
                    encoding="utf-8",
                )
            except (BenchmarkInfrastructureError, OSError) as cleanup_error:
                print(
                    "cleanup warning: could not capture "
                    f"{architecture} failure logs: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}",
                    file=sys.stderr,
                    flush=True,
                )
        try:
            driver.run("stop-all")
        except (BenchmarkInfrastructureError, OSError) as cleanup_error:
            print(
                "cleanup warning: could not stop all environments after "
                f"pair failure: {type(cleanup_error).__name__}: "
                f"{cleanup_error}",
                file=sys.stderr,
                flush=True,
            )
        checkpoint.invalidate_attempt(
            pair_key,
            reason_code="benchmark_infrastructure_failure",
            reason_message=f"{type(exc).__name__}: {exc}",
        )
        raise


def _iter_completed_attempt_dirs(
    checkpoint: BatchCheckpoint,
) -> Iterable[tuple[str, dict[str, object], Path]]:
    for pair_key, attempt_id in sorted(
        checkpoint.progress["completed_pairs"].items()
    ):
        attempts = checkpoint.progress["attempts"][pair_key]
        attempt = next(
            item for item in attempts if item["attempt_id"] == attempt_id
        )
        yield (
            pair_key,
            attempt,
            checkpoint.output_dir / "attempts" / pair_key / attempt_id,
        )


def _aggregate_csv(
    destination: Path,
    sources: Sequence[Path],
) -> None:
    fieldnames: list[str] | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as output:
        writer = None
        for source in sources:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if fieldnames is None:
                    fieldnames = list(reader.fieldnames or [])
                    writer = csv.DictWriter(output, fieldnames=fieldnames)
                    writer.writeheader()
                elif list(reader.fieldnames or []) != fieldnames:
                    raise ValueError(f"CSV schema mismatch in {source}")
                assert writer is not None
                writer.writerows(reader)
        if fieldnames is None:
            raise ValueError(f"no sources for {destination.name}")


def _exclusions(checkpoint: BatchCheckpoint) -> tuple[Exclusion, ...]:
    rows = []
    for pair_key, attempts in checkpoint.progress["attempts"].items():
        for attempt in attempts:
            if attempt["status"] != "invalid":
                continue
            rows.append(
                Exclusion(
                    batch_id=checkpoint.config.batch_id,
                    pair_id=f"{checkpoint.config.batch_id}-{pair_key}",
                    repetition=int(attempt["repetition"]),
                    attempt_id=str(attempt["attempt_id"]),
                    workload_size=int(attempt["workload_size"]),
                    concurrency=int(attempt["concurrency"]),
                    reason_code=str(attempt["reason_code"]),
                    reason_message=str(attempt["reason_message"]),
                    invalidated_at_utc=str(attempt["ended_at_utc"]),
                )
            )
    return tuple(rows)


def _fabric_blocks(requests_path: Path) -> tuple[FabricBlock, ...]:
    grouped: Counter[tuple[str, ...]] = Counter()
    with requests_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (
                row["architecture"] == "fabric"
                and row["operation"] in {"OP1", "OP3", "OP4"}
                and row["correctness_result"].lower() == "true"
                and row["fabric_block_number"]
            ):
                grouped[
                    (
                        row["batch_id"],
                        row["pair_id"],
                        row["repetition"],
                        row["attempt_id"],
                        row["run_id"],
                        row["operation"],
                        row["workload_size"],
                        row["concurrency"],
                        row["fabric_block_number"],
                    )
                ] += 1
    return tuple(
        FabricBlock(
            batch_id=key[0],
            pair_id=key[1],
            repetition=int(key[2]),
            attempt_id=key[3],
            run_id=key[4],
            operation=key[5],
            workload_size=int(key[6]),
            concurrency=int(key[7]),
            block_number=int(key[8]),
            observed_valid_transactions=count,
        )
        for key, count in sorted(grouped.items())
    )


def aggregate_batch(checkpoint: BatchCheckpoint) -> None:
    completed = list(_iter_completed_attempt_dirs(checkpoint))
    if not completed:
        return
    names = (
        "requests.csv",
        "runs.csv",
        "resources.csv",
        "storage.csv",
        "correctness.csv",
        "block_boundaries.csv",
    )
    for name in names:
        sources = [
            attempt_dir / architecture / name
            for _, _, attempt_dir in completed
            for architecture in ("traditional", "fabric")
        ]
        _aggregate_csv(checkpoint.output_dir / name, sources)

    inputs = [
        InputDataset(**json.loads((path / "input.json").read_text()))
        for _, _, path in completed
    ]
    _write_dataclass_rows(
        checkpoint.output_dir / "inputs.csv", inputs, InputDataset
    )
    _write_dataclass_rows(
        checkpoint.output_dir / "exclusions.csv",
        _exclusions(checkpoint),
        Exclusion,
    )
    _write_dataclass_rows(
        checkpoint.output_dir / "fabric_blocks.csv",
        _fabric_blocks(checkpoint.output_dir / "requests.csv"),
        FabricBlock,
    )
    live_configs = [
        json.loads(
            (attempt_dir / "fabric" / "fabric_channel_config.json").read_text(
                encoding="utf-8"
            )
        )
        for _, _, attempt_dir in completed
    ]
    if any(value != live_configs[0] for value in live_configs[1:]):
        raise ValueError("live Fabric channel configuration changed")
    checkpoint.manifest["live_fabric_channel_configuration"] = live_configs[0]
    observed_images: dict[str, dict[str, object]] = {}
    for _, _, attempt_dir in completed:
        for architecture in ("traditional", "fabric"):
            values = json.loads(
                (
                    attempt_dir
                    / architecture
                    / "runtime_container_images.json"
                ).read_text(encoding="utf-8")
            )
            for value in values:
                key = f"{value.get('image_id')}:{value.get('configured_image')}"
                observed_images[key] = value
    checkpoint.manifest["runtime_container_images_observed"] = [
        observed_images[key] for key in sorted(observed_images)
    ]
    (checkpoint.output_dir / "tamper_trials.csv").write_text(
        "scenario,trial,outcome,notes\n",
        encoding="utf-8",
    )


def _write_hashes(output_dir: Path) -> None:
    entries = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            entries.append(f"{_sha256(path)}  {path.relative_to(output_dir)}")
    (output_dir / "SHA256SUMS").write_text(
        "\n".join(entries) + "\n",
        encoding="utf-8",
    )


def _csv_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def verify_batch(output_dir: Path) -> dict[str, object]:
    manifest = json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8")
    )
    matrix = manifest["workload_matrix"]
    sizes = [int(value) for value in matrix["workload_sizes"]]
    concurrencies = [int(value) for value in matrix["concurrencies"]]
    repetitions = int(matrix["repetitions"])
    expected_pairs = len(sizes) * len(concurrencies) * repetitions
    expected_runs = expected_pairs * 2 * len(OPERATION_ORDER)
    expected_requests = (
        sum(sizes)
        * len(concurrencies)
        * repetitions
        * 2
        * len(OPERATION_ORDER)
    )
    expected_correctness = (
        sum(sizes) * len(concurrencies) * repetitions * 2 * 3
    )
    expected_storage = expected_pairs * 7
    counts = {
        name: _csv_count(output_dir / name)
        for name in (
            "requests.csv",
            "runs.csv",
            "resources.csv",
            "storage.csv",
            "correctness.csv",
            "inputs.csv",
            "exclusions.csv",
            "block_boundaries.csv",
            "fabric_blocks.csv",
        )
    }
    if counts["requests.csv"] != expected_requests:
        raise ValueError("unexpected measured request count")
    if counts["runs.csv"] != expected_runs:
        raise ValueError("unexpected run count")
    if counts["correctness.csv"] != expected_correctness:
        raise ValueError("unexpected correctness row count")
    if counts["inputs.csv"] != expected_pairs:
        raise ValueError("unexpected input dataset count")
    if counts["storage.csv"] != expected_storage:
        raise ValueError("unexpected storage row count")
    if counts["block_boundaries.csv"] != expected_runs:
        raise ValueError("unexpected block-boundary count")
    if counts["resources.csv"] < expected_runs * 2:
        raise ValueError("resource samples are incomplete")

    progress = json.loads(
        (output_dir / "progress.json").read_text(encoding="utf-8")
    )
    if manifest.get("status") != "completed" or progress.get("status") != "completed":
        raise ValueError("batch is not completed")
    if len(progress.get("completed_pairs", {})) != expected_pairs:
        raise ValueError("completed-pair checkpoint count is incorrect")
    live_config = manifest.get("live_fabric_channel_configuration", {})
    if (
        live_config.get("batch_timeout") != "2s"
        or int(live_config.get("max_message_count", -1)) != 10
    ):
        raise ValueError("unexpected live Fabric block-cutting configuration")

    expected_run_keys = {
        (str(size), str(concurrency), str(repetition), architecture, operation.value)
        for size in sizes
        for concurrency in concurrencies
        for repetition in range(1, repetitions + 1)
        for architecture in ("traditional", "fabric")
        for operation in OPERATION_ORDER
    }
    run_keys: Counter[tuple[str, str, str, str, str]] = Counter()
    run_workloads: dict[str, int] = {}
    with (output_dir / "runs.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        for row in csv.DictReader(handle):
            key = (
                row["workload_size"],
                row["concurrency"],
                row["repetition"],
                row["architecture"],
                row["operation"],
            )
            run_keys[key] += 1
            run_workloads[row["run_id"]] = int(row["workload_size"])
            if int(row["attempted_requests"]) != int(row["workload_size"]):
                raise ValueError("run attempted-request count is incorrect")
    if set(run_keys) != expected_run_keys or any(
        count != 1 for count in run_keys.values()
    ):
        raise ValueError("run matrix is incomplete or duplicated")
    run_ids = set(run_workloads)
    resource_phases: dict[str, set[str]] = defaultdict(set)
    with (output_dir / "resources.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        for row in csv.DictReader(handle):
            resource_phases[row["run_id"]].add(row["sample_phase"])
    if set(resource_phases) != run_ids or any(
        phases != {"idle_baseline", "measured"}
        for phases in resource_phases.values()
    ):
        raise ValueError("resource phase coverage is incomplete")

    with (output_dir / "block_boundaries.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        boundary_counts = Counter(
            row["run_id"] for row in csv.DictReader(handle)
        )
    if set(boundary_counts) != run_ids or any(
        count != 1 for count in boundary_counts.values()
    ):
        raise ValueError("block-boundary coverage is incomplete")

    request_counts: Counter[str] = Counter()
    with (output_dir / "requests.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        for row in csv.DictReader(handle):
            request_counts[row["run_id"]] += 1
            if (
                row["architecture"] == "fabric"
                and row["operation"] in {"OP1", "OP3", "OP4"}
                and row["correctness_result"].lower() == "true"
                and (
                    not row["fabric_transaction_id"]
                    or row["fabric_commit_validation_status"] != "VALID"
                    or not row["fabric_block_number"]
                )
            ):
                raise ValueError("successful Fabric write lacks commit metadata")
    if set(request_counts) != run_ids or any(
        request_counts[run_id] != run_workloads[run_id]
        for run_id in run_ids
    ):
        raise ValueError("request-to-run cardinality is incorrect")

    correctness_failures = 0
    with (output_dir / "correctness.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        correctness_rows = list(csv.DictReader(handle))
    correctness_failures = sum(
        row["correctness_result"].lower() != "true"
        for row in correctness_rows
    )
    correctness_keys: Counter[tuple[str, str, str, str, str]] = Counter(
        (
            row["workload_size"],
            row["concurrency"],
            row["repetition"],
            row["architecture"],
            row["validation_operation"],
        )
        for row in correctness_rows
    )
    expected_correctness_keys = {
        (str(size), str(concurrency), str(repetition), architecture, operation)
        for size in sizes
        for concurrency in concurrencies
        for repetition in range(1, repetitions + 1)
        for architecture in ("traditional", "fabric")
        for operation in ("OP2", "OP5", "OP6")
    }
    if set(correctness_keys) != expected_correctness_keys or any(
        count != int(key[0]) for key, count in correctness_keys.items()
    ):
        raise ValueError("durable correctness matrix is incomplete")

    hashes = (output_dir / "SHA256SUMS").read_text(
        encoding="utf-8"
    ).splitlines()
    for line in hashes:
        expected_hash, relative = line.split("  ", 1)
        if _sha256(output_dir / relative) != expected_hash:
            raise ValueError(f"hash mismatch: {relative}")

    return {
        "expected_pairs": expected_pairs,
        "counts": counts,
        "correctness_failures": correctness_failures,
        "correctness_gate": (
            "PASS" if correctness_failures == 0 else "FAIL"
        ),
    }


def _freeze(output_dir: Path) -> None:
    for path in sorted(output_dir.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    output_dir.chmod(0o555)


async def run_final(config: FinalConfig) -> Path:
    initial_driver = EnvironmentDriver(config.driver, config.output_root)
    initial_driver.run("preflight")
    initial_driver.run("stop-all")
    checkpoint = BatchCheckpoint(config)
    driver = EnvironmentDriver(config.driver, checkpoint.output_dir)
    checkpoint.manifest["status"] = "running"
    checkpoint.persist()
    try:
        for workload_size in config.workload_sizes:
            for concurrency in config.concurrencies:
                for repetition in range(1, config.repetitions + 1):
                    pair_key = _pair_key(
                        workload_size, concurrency, repetition
                    )
                    if checkpoint.is_complete(pair_key):
                        print(f"resume skip completed {pair_key}", flush=True)
                        continue
                    print(f"begin {pair_key}", flush=True)
                    await _run_pair(
                        config,
                        checkpoint,
                        driver,
                        workload_size=workload_size,
                        concurrency=concurrency,
                        repetition=repetition,
                    )
    except BaseException:
        aggregate_batch(checkpoint)
        raise
    finally:
        try:
            driver.run("stop-all")
        except (BenchmarkInfrastructureError, OSError) as cleanup_error:
            print(
                "cleanup warning: could not perform final environment "
                f"shutdown: {type(cleanup_error).__name__}: "
                f"{cleanup_error}",
                file=sys.stderr,
                flush=True,
            )

    aggregate_batch(checkpoint)
    checkpoint.progress["status"] = "completed"
    checkpoint.manifest["status"] = "completed"
    checkpoint.manifest["ended_at_utc"] = _utc_now().isoformat()
    checkpoint.persist()
    _write_hashes(checkpoint.output_dir)
    summary = verify_batch(checkpoint.output_dir)
    _atomic_json(checkpoint.output_dir / "verification.json", summary)
    _write_hashes(checkpoint.output_dir)
    verify_batch(checkpoint.output_dir)
    _freeze(checkpoint.output_dir)
    return checkpoint.output_dir


def _default_batch_id() -> str:
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    try:
        commit = _git("rev-parse", "--short", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        commit = "unknown"
    return f"final-{timestamp}-{commit}"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or resume the locked final experiment matrix."
    )
    parser.add_argument("--batch-id", default=_default_batch_id())
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--traditional-url", default="http://127.0.0.1:18000"
    )
    parser.add_argument("--fabric-url", default="http://127.0.0.1:18001")
    parser.add_argument(
        "--workload-sizes", nargs="+", type=int, default=(100, 500, 1000)
    )
    parser.add_argument(
        "--concurrencies", nargs="+", type=int, default=(1, 5, 10)
    )
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--resource-sampling-interval-seconds", type=float, default=1.0
    )
    parser.add_argument(
        "--output-root", type=Path, default=Path("results/raw")
    )
    parser.add_argument(
        "--driver",
        type=Path,
        default=Path("benchmark/final_environment.sh"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    output_dir = arguments.output_root / arguments.batch_id
    if arguments.verify_only:
        print(json.dumps(verify_batch(output_dir), indent=2, sort_keys=True))
        print("FINAL_ARTIFACTS_VALID")
        return 0
    config = FinalConfig(
        batch_id=arguments.batch_id,
        traditional_url=arguments.traditional_url,
        fabric_url=arguments.fabric_url,
        workload_sizes=tuple(arguments.workload_sizes),
        concurrencies=tuple(arguments.concurrencies),
        repetitions=arguments.repetitions,
        seed=arguments.seed,
        timeout_seconds=arguments.timeout_seconds,
        resource_sampling_interval_seconds=(
            arguments.resource_sampling_interval_seconds
        ),
        output_root=arguments.output_root,
        driver=arguments.driver,
        resume=arguments.resume,
        smoke=arguments.smoke,
    )
    completed = asyncio.run(run_final(config))
    print(f"FINAL_OUTPUT={completed}")
    print("PHASE4_FINAL_COMPLETED_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
