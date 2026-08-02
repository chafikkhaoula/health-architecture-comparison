from benchmark.resources import (
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
