import asyncio
import json

from benchmark.resources import (
    DockerStatsMonitor,
    ResourceContext,
    docker_container_ids,
    parse_docker_stats,
    parse_size,
)


def _context() -> ResourceContext:
    return ResourceContext(
        batch_id="final-test",
        pair_id="pair-test",
        repetition=1,
        attempt_id="attempt-test",
        architecture="fabric",
        run_id="run-test",
        operation="OP1",
        workload_size=100,
        concurrency=10,
    )


def test_parse_size_supports_binary_and_decimal_units() -> None:
    assert parse_size("1.5MiB") == 1_572_864
    assert parse_size("2 GB") == 2_000_000_000
    assert parse_size("0B") == 0
    assert parse_size(" 1e+03kB") == 1_000_000


def test_parse_docker_stats_preserves_numeric_units() -> None:
    sample = parse_docker_stats(
        """{
          "ID":"abc123",
          "Name":"peer0",
          "CPUPerc":"125.5%",
          "MemUsage":"64MiB / 2GiB",
          "MemPerc":"3.125%",
          "NetIO":"1.2kB / 3.4kB",
          "BlockIO":"5MB / 6MB",
          "PIDs":"17"
        }""",
        _context(),
        sample_phase="measured",
        sampled_at_utc="2026-08-02T00:00:00+00:00",
        monotonic_ns=123,
    )

    assert sample.container_id == "abc123"
    assert sample.component_kind == "container"
    assert sample.component_available is True
    assert sample.cpu_percent == 125.5
    assert sample.memory_usage_bytes == 64 * 1024**2
    assert sample.memory_limit_bytes == 2 * 1024**3
    assert sample.network_input_bytes == 1200
    assert sample.block_output_bytes == 6_000_000
    assert sample.pids == 17


def test_docker_container_ids_only_discovers_running_containers(
    monkeypatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    outputs = iter(("compose-id\nshared-id\n", "chaincode-id\nshared-id\n"))

    def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        return type(
            "Completed",
            (),
            {"stdout": next(outputs)},
        )()

    monkeypatch.setattr("benchmark.resources.subprocess.run", fake_run)

    assert docker_container_ids("fabric") == (
        "chaincode-id",
        "compose-id",
        "shared-id",
    )
    assert len(calls) == 2
    assert all("--all" not in call for call in calls)
    assert all("status=running" in call for call in calls)


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    async def readline(self) -> bytes:
        return next(self._lines, b"")


class _FakeProcess:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)


class _FakeCleanupProcess:
    def __init__(
        self,
        *,
        communicate_stalls_until_killed: bool = False,
    ) -> None:
        self.stdout = _FakeStdout([])
        self.returncode: int | None = None
        self.communicate_stalls_until_killed = (
            communicate_stalls_until_killed
        )
        self.terminate_called = False
        self.kill_called = False
        self.communicate_calls = 0

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.communicate_stalls_until_killed and not self.kill_called:
            await asyncio.Event().wait()
        self.returncode = self.returncode if self.returncode is not None else -15
        return b"", b""


def test_docker_stats_monitor_stop_cancels_stalled_reader() -> None:
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )

    async def exercise() -> None:
        reader_started = asyncio.Event()

        async def stalled_reader() -> None:
            reader_started.set()
            await asyncio.Event().wait()

        monitor._reader = asyncio.create_task(stalled_reader())
        await reader_started.wait()
        await asyncio.wait_for(monitor._stop(), timeout=0.5)
        assert monitor._reader is None

    asyncio.run(exercise())


def test_docker_stats_monitor_stop_drains_pipe_on_normal_exit() -> None:
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )
    process = _FakeCleanupProcess()
    monitor._process = process  # type: ignore[assignment]

    asyncio.run(monitor._stop())

    assert process.terminate_called is True
    assert process.kill_called is False
    assert process.communicate_calls == 1
    assert monitor._process is None


def test_docker_stats_monitor_stop_escalates_to_sigkill(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "benchmark.resources._PROCESS_STOP_TIMEOUT_SECONDS",
        0.01,
    )
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )
    process = _FakeCleanupProcess(
        communicate_stalls_until_killed=True,
    )
    monitor._process = process  # type: ignore[assignment]

    asyncio.run(monitor._stop())

    assert process.terminate_called is True
    assert process.kill_called is True
    assert process.communicate_calls == 2
    assert monitor._process is None


def test_docker_stats_monitor_ignores_blank_and_ansi_stream_frames() -> None:
    payload = b"""{
      "ID":"abc123",
      "Name":"peer0",
      "CPUPerc":"1%",
      "MemUsage":"1MiB / 2GiB",
      "MemPerc":"0.1%",
      "NetIO":"0B / 0B",
      "BlockIO":"0B / 0B",
      "PIDs":"1"
    }\n"""
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )
    framed_payload = (
        b"\x1b[J\x1b[H" + payload.rstrip(b"\n") + b"\x1b[K\n"
    )
    monitor._process = _FakeProcess(  # type: ignore[assignment]
        [b"\n", b" \r\n", b"\x1b[K\n", framed_payload]
    )

    asyncio.run(monitor._read())

    assert monitor._reader_error is None
    assert len(monitor.samples) == 1
    assert monitor.samples[0].container_id == "abc123"
    assert monitor._baseline_ids == {"abc123"}


def test_docker_stats_monitor_rejects_malformed_non_control_frame() -> None:
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )
    monitor._process = _FakeProcess(  # type: ignore[assignment]
        [b"\x1b[Hnot-json\x1b[K\n"]
    )

    asyncio.run(monitor._read())

    assert isinstance(monitor._reader_error, json.JSONDecodeError)
    assert monitor.samples == []
    assert monitor._baseline_ids == set()


def test_docker_stats_monitor_cleans_up_when_enter_fails(
    monkeypatch,
) -> None:
    monitor = DockerStatsMonitor(
        _context(),
        ("abc123",),
        sample_interval_seconds=1,
    )
    stopped = False

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return _FakeProcess([])

    async def fail_wait(*_args, **_kwargs) -> None:
        raise RuntimeError("baseline failed")

    async def record_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(monitor, "_wait_for_ids", fail_wait)
    monkeypatch.setattr(monitor, "_stop", record_stop)

    async def enter() -> None:
        try:
            await monitor.__aenter__()
        except RuntimeError as exc:
            assert str(exc) == "baseline failed"
        else:
            raise AssertionError("monitor entry unexpectedly succeeded")

    asyncio.run(enter())

    assert stopped is True
