from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns


@dataclass(frozen=True, slots=True)
class ResourceContext:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    operation: str
    workload_size: int
    concurrency: int


@dataclass(frozen=True, slots=True)
class ResourceSample:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    run_id: str
    operation: str
    workload_size: int
    concurrency: int
    sample_phase: str
    sampled_at_utc: str
    monotonic_ns: int
    component_kind: str
    component_available: bool
    container_id: str
    container_name: str
    cpu_percent: float
    memory_usage_bytes: int
    memory_limit_bytes: int
    memory_percent: float
    network_input_bytes: int
    network_output_bytes: int
    block_input_bytes: int
    block_output_bytes: int
    pids: int | None


@dataclass(frozen=True, slots=True)
class StorageObservation:
    batch_id: str
    pair_id: str
    repetition: int
    attempt_id: str
    architecture: str
    workload_size: int
    concurrency: int
    component: str
    storage_kind: str
    bytes_used: int
    component_available: bool
    error_message: str | None
    measured_at_utc: str


_SIZE_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)\s*$",
    re.IGNORECASE,
)
_SIZE_FACTORS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "pb": 1000**5,
    "eb": 1000**6,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
    "eib": 1024**6,
}


def parse_size(value: str) -> int:
    match = _SIZE_RE.match(value)
    if match is None:
        raise ValueError(f"unrecognized Docker size: {value!r}")
    magnitude, unit = match.groups()
    return round(float(magnitude) * _SIZE_FACTORS[unit.lower()])


def _parse_pair(value: str) -> tuple[int, int]:
    left, separator, right = value.partition("/")
    if not separator:
        raise ValueError(f"unrecognized Docker I/O pair: {value!r}")
    return parse_size(left), parse_size(right)


def _parse_percent(value: str) -> float:
    return float(value.strip().removesuffix("%"))


def parse_docker_stats(
    payload: str,
    context: ResourceContext,
    *,
    sample_phase: str,
    sampled_at_utc: str,
    monotonic_ns: int,
) -> ResourceSample:
    data = json.loads(payload)
    memory_usage, memory_limit = _parse_pair(data["MemUsage"])
    network_input, network_output = _parse_pair(data["NetIO"])
    block_input, block_output = _parse_pair(data["BlockIO"])
    raw_pids = str(data.get("PIDs", "")).strip()
    return ResourceSample(
        **asdict(context),
        sample_phase=sample_phase,
        sampled_at_utc=sampled_at_utc,
        monotonic_ns=monotonic_ns,
        component_kind="container",
        component_available=True,
        container_id=str(data.get("ID") or data.get("Container")),
        container_name=str(data["Name"]),
        cpu_percent=_parse_percent(data["CPUPerc"]),
        memory_usage_bytes=memory_usage,
        memory_limit_bytes=memory_limit,
        memory_percent=_parse_percent(data["MemPerc"]),
        network_input_bytes=network_input,
        network_output_bytes=network_output,
        block_input_bytes=block_input,
        block_output_bytes=block_output,
        pids=int(raw_pids) if raw_pids.isdigit() else None,
    )


def docker_container_ids(architecture: str) -> tuple[str, ...]:
    if architecture not in {"traditional", "fabric"}:
        raise ValueError("unsupported architecture")
    filters = [
        "--filter",
        (
            "label=com.docker.compose.project="
            f"health-arch-{architecture}"
        ),
    ]
    completed = subprocess.run(
        ("docker", "ps", "--all", *filters, "--format", "{{.ID}}"),
        check=True,
        capture_output=True,
        text=True,
    )
    identifiers = {
        item.strip()
        for item in completed.stdout.splitlines()
        if item.strip()
    }
    if architecture == "fabric":
        network = subprocess.run(
            (
                "docker",
                "ps",
                "--all",
                "--filter",
                "network=health-arch-fabric-net",
                "--format",
                "{{.ID}}",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        identifiers.update(
            item.strip()
            for item in network.stdout.splitlines()
            if item.strip()
        )
    if not identifiers:
        raise RuntimeError(
            f"no running containers found for {architecture}"
        )
    return tuple(sorted(identifiers))


def managed_api_pid(architecture: str) -> int:
    path = Path("tmp/benchmark-services") / f"{architecture}.pid"
    raw_value = path.read_text(encoding="utf-8").strip()
    if not raw_value.isdigit():
        raise RuntimeError(f"invalid managed API PID in {path}")
    return int(raw_value)


class DockerStatsMonitor:
    """Capture an idle sample and at least one in-boundary sample."""

    def __init__(
        self,
        context: ResourceContext,
        container_ids: Sequence[str],
        *,
        sample_interval_seconds: float,
        process_ids: dict[str, int] | None = None,
    ) -> None:
        if not container_ids:
            raise ValueError("container_ids must not be empty")
        if sample_interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        self.context = context
        self.container_ids = tuple(container_ids)
        self.sample_interval_seconds = sample_interval_seconds
        self.process_ids = dict(process_ids or {})
        self.samples: list[ResourceSample] = []
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._baseline_ids: set[str] = set()
        self._measured_ids: set[str] = set()
        self._measurement_started_ns: int | None = None
        self._sample_event = asyncio.Event()
        self._reader_error: BaseException | None = None
        self._last_process_sample_ns: int | None = None
        self._process_cpu_state: dict[int, tuple[int, int]] = {}

    async def __aenter__(self) -> DockerStatsMonitor:
        self._process = await asyncio.create_subprocess_exec(
            "docker",
            "stats",
            "--format",
            "{{json .}}",
            *self.container_ids,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())
        await self._wait_for_ids(self._baseline_ids, "idle baseline")
        return self

    async def __aexit__(self, *_: object) -> None:
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        reader = self._reader
        if reader is not None:
            await reader
        if self._reader_error is not None:
            raise RuntimeError("Docker resource monitor failed") from (
                self._reader_error
            )

    def mark_measurement_started(self) -> None:
        self._measurement_started_ns = perf_counter_ns()
        self._sample_event.clear()

    async def wait_for_measured_sample(self) -> None:
        await self._wait_for_ids(
            self._measured_ids,
            "measured resource",
        )

    async def _wait_for_ids(
        self,
        observed: set[str],
        label: str,
    ) -> None:
        expected = set(self.container_ids) | {
            f"process:{pid}" for pid in self.process_ids.values()
        }
        timeout = max(10.0, self.sample_interval_seconds * 4)
        try:
            async with asyncio.timeout(timeout):
                while not expected.issubset(observed):
                    if self._reader_error is not None:
                        raise RuntimeError(f"{label} sampling failed") from (
                            self._reader_error
                        )
                    await self._sample_event.wait()
                    self._sample_event.clear()
        except TimeoutError:
            self._record_unavailable_components(expected - observed, observed)
            if not expected.issubset(observed):
                raise RuntimeError(f"{label} sampling timed out")

    async def _read(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                sampled_ns = perf_counter_ns()
                phase = (
                    "idle_baseline"
                    if self._measurement_started_ns is None
                    or sampled_ns < self._measurement_started_ns
                    else "measured"
                )
                sample = parse_docker_stats(
                    line.decode("utf-8"),
                    self.context,
                    sample_phase=phase,
                    sampled_at_utc=datetime.now(timezone.utc).isoformat(),
                    monotonic_ns=sampled_ns,
                )
                self.samples.append(sample)
                target = (
                    self._baseline_ids
                    if phase == "idle_baseline"
                    else self._measured_ids
                )
                target.add(sample.container_id)
                self._sample_processes(
                    sample_phase=phase,
                    sampled_at_utc=sample.sampled_at_utc,
                    monotonic_ns=sampled_ns,
                    target=target,
                )
                self._sample_event.set()
        except BaseException as exc:
            self._reader_error = exc
            self._sample_event.set()

    def _sample_processes(
        self,
        *,
        sample_phase: str,
        sampled_at_utc: str,
        monotonic_ns: int,
        target: set[str],
    ) -> None:
        minimum_gap_ns = int(
            self.sample_interval_seconds * 0.5 * 1_000_000_000
        )
        if (
            self._last_process_sample_ns is not None
            and monotonic_ns - self._last_process_sample_ns < minimum_gap_ns
        ):
            return
        self._last_process_sample_ns = monotonic_ns
        clock_ticks = os.sysconf("SC_CLK_TCK")
        memory_limit = os.sysconf("SC_PAGE_SIZE") * os.sysconf(
            "SC_PHYS_PAGES"
        )
        for name, pid in self.process_ids.items():
            try:
                stat_fields = Path(f"/proc/{pid}/stat").read_text(
                    encoding="utf-8"
                ).split()
                status_lines = Path(f"/proc/{pid}/status").read_text(
                    encoding="utf-8"
                ).splitlines()
            except FileNotFoundError:
                self._append_unavailable_process(
                    name,
                    pid,
                    sample_phase=sample_phase,
                    sampled_at_utc=sampled_at_utc,
                    monotonic_ns=monotonic_ns,
                    target=target,
                )
                continue
            process_ticks = int(stat_fields[13]) + int(stat_fields[14])
            previous = self._process_cpu_state.get(pid)
            cpu_percent = 0.0
            if previous is not None:
                previous_ticks, previous_ns = previous
                elapsed_seconds = (monotonic_ns - previous_ns) / 1_000_000_000
                if elapsed_seconds > 0:
                    cpu_percent = (
                        (process_ticks - previous_ticks)
                        / clock_ticks
                        / elapsed_seconds
                        * 100
                    )
            self._process_cpu_state[pid] = (process_ticks, monotonic_ns)
            rss_bytes = 0
            for line in status_lines:
                if line.startswith("VmRSS:"):
                    rss_bytes = int(line.split()[1]) * 1024
                    break
            identifier = f"process:{pid}"
            self.samples.append(
                ResourceSample(
                    **asdict(self.context),
                    sample_phase=sample_phase,
                    sampled_at_utc=sampled_at_utc,
                    monotonic_ns=monotonic_ns,
                    component_kind="process",
                    component_available=True,
                    container_id=identifier,
                    container_name=name,
                    cpu_percent=cpu_percent,
                    memory_usage_bytes=rss_bytes,
                    memory_limit_bytes=memory_limit,
                    memory_percent=(rss_bytes / memory_limit * 100),
                    network_input_bytes=0,
                    network_output_bytes=0,
                    block_input_bytes=0,
                    block_output_bytes=0,
                    pids=1,
                )
            )
            target.add(identifier)

    def _append_unavailable_process(
        self,
        name: str,
        pid: int,
        *,
        sample_phase: str,
        sampled_at_utc: str,
        monotonic_ns: int,
        target: set[str],
    ) -> None:
        identifier = f"process:{pid}"
        self.samples.append(
            ResourceSample(
                **asdict(self.context),
                sample_phase=sample_phase,
                sampled_at_utc=sampled_at_utc,
                monotonic_ns=monotonic_ns,
                component_kind="process",
                component_available=False,
                container_id=identifier,
                container_name=name,
                cpu_percent=0.0,
                memory_usage_bytes=0,
                memory_limit_bytes=(
                    os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                ),
                memory_percent=0.0,
                network_input_bytes=0,
                network_output_bytes=0,
                block_input_bytes=0,
                block_output_bytes=0,
                pids=0,
            )
        )
        target.add(identifier)

    def _record_unavailable_components(
        self,
        identifiers: set[str],
        target: set[str],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        monotonic_ns = perf_counter_ns()
        phase = (
            "idle_baseline"
            if self._measurement_started_ns is None
            else "measured"
        )
        process_names = {
            f"process:{pid}": (name, pid)
            for name, pid in self.process_ids.items()
        }
        for identifier in identifiers:
            if identifier in process_names:
                name, pid = process_names[identifier]
                if not Path(f"/proc/{pid}").exists():
                    self._append_unavailable_process(
                        name,
                        pid,
                        sample_phase=phase,
                        sampled_at_utc=now,
                        monotonic_ns=monotonic_ns,
                        target=target,
                    )
                continue
            completed = subprocess.run(
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}} {{.Name}}",
                    identifier,
                ),
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                continue
            running, _, name = completed.stdout.strip().partition(" ")
            if running == "true":
                continue
            self.samples.append(
                ResourceSample(
                    **asdict(self.context),
                    sample_phase=phase,
                    sampled_at_utc=now,
                    monotonic_ns=monotonic_ns,
                    component_kind="container",
                    component_available=False,
                    container_id=identifier,
                    container_name=name.removeprefix("/"),
                    cpu_percent=0.0,
                    memory_usage_bytes=0,
                    memory_limit_bytes=0,
                    memory_percent=0.0,
                    network_input_bytes=0,
                    network_output_bytes=0,
                    block_input_bytes=0,
                    block_output_bytes=0,
                    pids=0,
                )
            )
            target.add(identifier)


def capture_storage(
    context: ResourceContext,
) -> tuple[StorageObservation, ...]:
    paths = {
        "traditional": {
            "postgres": ("postgresql", "/var/lib/postgresql/data"),
        },
        "fabric": {
            "postgres": ("off_chain_postgresql", "/var/lib/postgresql/data"),
            "orderer": ("ledger", "/var/hyperledger/production/orderer"),
            "peer0org1": ("ledger_and_state", "/var/hyperledger/production"),
            "peer0org2": ("ledger_and_state", "/var/hyperledger/production"),
        },
    }[context.architecture]
    rows: list[StorageObservation] = []

    for service, (kind, path) in paths.items():
        resolved = subprocess.run(
            (
                "docker",
                "ps",
                "--filter",
                (
                    "label=com.docker.compose.project="
                    f"health-arch-{context.architecture}"
                ),
                "--filter",
                f"label=com.docker.compose.service={service}",
                "--format",
                "{{.ID}}",
            ),
            capture_output=True,
            text=True,
        )
        if resolved.returncode != 0:
            raise RuntimeError(
                f"Docker storage discovery failed for {context.architecture}"
            )
        identifiers = [
            item.strip()
            for item in resolved.stdout.splitlines()
            if item.strip()
        ]
        if len(identifiers) != 1:
            rows.append(
                StorageObservation(
                    batch_id=context.batch_id,
                    pair_id=context.pair_id,
                    repetition=context.repetition,
                    attempt_id=context.attempt_id,
                    architecture=context.architecture,
                    workload_size=context.workload_size,
                    concurrency=context.concurrency,
                    component=service,
                    storage_kind=kind,
                    bytes_used=0,
                    component_available=False,
                    error_message="component container unavailable",
                    measured_at_utc=datetime.now(timezone.utc).isoformat(),
                )
            )
            continue
        identifier = identifiers[0]
        completed = subprocess.run(
            ("docker", "exec", identifier, "du", "-sk", path),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            rows.append(
                StorageObservation(
                    batch_id=context.batch_id,
                    pair_id=context.pair_id,
                    repetition=context.repetition,
                    attempt_id=context.attempt_id,
                    architecture=context.architecture,
                    workload_size=context.workload_size,
                    concurrency=context.concurrency,
                    component=service,
                    storage_kind=kind,
                    bytes_used=0,
                    component_available=False,
                    error_message="storage path unavailable",
                    measured_at_utc=datetime.now(timezone.utc).isoformat(),
                )
            )
            continue
        try:
            kibibytes = int(completed.stdout.split()[0])
        except (IndexError, ValueError):
            rows.append(
                StorageObservation(
                    batch_id=context.batch_id,
                    pair_id=context.pair_id,
                    repetition=context.repetition,
                    attempt_id=context.attempt_id,
                    architecture=context.architecture,
                    workload_size=context.workload_size,
                    concurrency=context.concurrency,
                    component=service,
                    storage_kind=kind,
                    bytes_used=0,
                    component_available=False,
                    error_message="invalid storage measurement",
                    measured_at_utc=datetime.now(timezone.utc).isoformat(),
                )
            )
            continue
        rows.append(
            StorageObservation(
                batch_id=context.batch_id,
                pair_id=context.pair_id,
                repetition=context.repetition,
                attempt_id=context.attempt_id,
                architecture=context.architecture,
                workload_size=context.workload_size,
                concurrency=context.concurrency,
                component=service,
                storage_kind=kind,
                bytes_used=kibibytes * 1024,
                component_available=True,
                error_message=None,
                measured_at_utc=datetime.now(timezone.utc).isoformat(),
            )
        )

    rows.append(
        StorageObservation(
            batch_id=context.batch_id,
            pair_id=context.pair_id,
            repetition=context.repetition,
            attempt_id=context.attempt_id,
            architecture=context.architecture,
            workload_size=context.workload_size,
            concurrency=context.concurrency,
            component="architecture_total",
            storage_kind="sum_of_persistent_components",
            bytes_used=sum(row.bytes_used for row in rows),
            component_available=all(
                row.component_available for row in rows
            ),
            error_message=(
                None
                if all(row.component_available for row in rows)
                else "one or more persistent components unavailable"
            ),
            measured_at_utc=datetime.now(timezone.utc).isoformat(),
        )
    )
    return tuple(rows)


def _write_rows(path: Path, rows: Sequence[object], row_type: type) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in fields(row_type)]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def write_resource_samples(
    path: Path,
    rows: Sequence[ResourceSample],
) -> None:
    _write_rows(path, rows, ResourceSample)


def write_storage_observations(
    path: Path,
    rows: Sequence[StorageObservation],
) -> None:
    _write_rows(path, rows, StorageObservation)
