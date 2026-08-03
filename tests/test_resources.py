import asyncio
import json

from benchmark.resources import (
    DockerStatsMonitor,
    ResourceContext,
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


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    async def readline(self) -> bytes:
        return next(self._lines, b"")


class _FakeProcess:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)


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
