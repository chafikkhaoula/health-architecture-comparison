from pathlib import Path

import pytest

from benchmark.final import (
    BatchCheckpoint,
    FinalConfig,
    _namespace,
    _pair_key,
    _prime_if_fabric,
    _verify_code_provenance,
    architecture_order,
)


def test_final_matrix_identifiers_and_counterbalancing() -> None:
    assert architecture_order(1) == ("traditional", "fabric")
    assert architecture_order(2) == ("fabric", "traditional")
    assert _pair_key(1000, 10, 10) == "n1000-c10-r10"

    warmup = _namespace("batch", 100, 5, 1, 1, "warmup")
    measured = _namespace("batch", 100, 5, 1, 1, "measured")
    assert warmup != measured
    assert warmup == _namespace("batch", 100, 5, 1, 1, "warmup")
    assert len(warmup) <= 20


def test_resume_invalidates_only_interrupted_attempt() -> None:
    checkpoint = BatchCheckpoint.__new__(BatchCheckpoint)
    checkpoint.progress = {
        "attempts": {
            "n0100-c01-r01": [
                {
                    "attempt_id": "attempt01",
                    "status": "running",
                }
            ],
            "n0100-c01-r02": [
                {
                    "attempt_id": "attempt01",
                    "status": "completed",
                }
            ],
        },
        "completed_pairs": {
            "n0100-c01-r01": "attempt01",
            "n0100-c01-r02": "attempt01",
        },
    }
    persisted = []
    checkpoint.persist = lambda: persisted.append(True)  # type: ignore[method-assign]

    checkpoint._recover_interrupted()

    interrupted = checkpoint.progress["attempts"]["n0100-c01-r01"][0]
    assert interrupted["status"] == "invalid"
    assert interrupted["reason_code"] == "interrupted_attempt"
    assert "n0100-c01-r01" not in checkpoint.progress["completed_pairs"]
    assert checkpoint.progress["completed_pairs"]["n0100-c01-r02"] == (
        "attempt01"
    )
    assert persisted == [True]


def test_final_config_rejects_concurrency_above_smallest_workload(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        FinalConfig(
            batch_id="final-test",
            traditional_url="http://traditional.test",
            fabric_url="http://fabric.test",
            workload_sizes=(5,),
            concurrencies=(10,),
            repetitions=1,
            seed=42,
            timeout_seconds=1,
            resource_sampling_interval_seconds=1,
            output_root=tmp_path,
            driver=tmp_path / "driver.sh",
        )


def _resume_config(
    tmp_path: Path,
    *,
    monitoring_only_continuation: bool,
) -> FinalConfig:
    return FinalConfig(
        batch_id="final-test",
        traditional_url="http://traditional.test",
        fabric_url="http://fabric.test",
        workload_sizes=(100,),
        concurrencies=(1,),
        repetitions=1,
        seed=42,
        timeout_seconds=1,
        resource_sampling_interval_seconds=1,
        output_root=tmp_path,
        driver=tmp_path / "driver.sh",
        resume=True,
        monitoring_only_continuation=monitoring_only_continuation,
    )


def test_resume_records_monitoring_only_continuation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkpoint = BatchCheckpoint.__new__(BatchCheckpoint)
    checkpoint.config = _resume_config(
        tmp_path,
        monitoring_only_continuation=True,
    )
    checkpoint.output_dir = tmp_path
    checkpoint.manifest = {
        "git_commit": "base-commit",
        "status": "paused_infrastructure_failure",
        "workload_matrix": {
            "workload_sizes": [100],
            "concurrencies": [1],
            "repetitions": 1,
        },
    }
    checkpoint.progress = {
        "attempts": {
            "n0100-c01-r01": [
                {"attempt_id": "attempt01", "status": "invalid"}
            ]
        },
        "completed_pairs": {},
        "updated_at_utc": "2026-08-08T00:00:00+00:00",
    }
    persisted: list[bool] = []
    checkpoint.persist = lambda: persisted.append(True)  # type: ignore[method-assign]

    def fake_git(*arguments: str) -> str:
        if arguments == ("status", "--porcelain"):
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return "continuation-commit"
        if arguments[:2] == ("diff", "--name-only"):
            return "benchmark/resources.py\ntests/test_resources.py"
        if arguments[:2] == ("diff", "--binary"):
            return "monitoring patch"
        raise AssertionError(arguments)

    monkeypatch.setattr("benchmark.final._git", fake_git)
    monkeypatch.setattr(
        "benchmark.final._source_hashes",
        lambda: {"benchmark/resources.py": "sha256"},
    )

    checkpoint._validate_resume()

    segments = checkpoint.manifest["code_provenance"]["segments"]
    assert len(segments) == 2
    assert segments[1]["git_commit"] == "continuation-commit"
    assert segments[1]["change_class"] == "monitoring_only"
    assert checkpoint.execution_git_commit == "continuation-commit"
    assert checkpoint.provenance_segment_id == (
        "monitoring-continuation-01"
    )
    old_attempt = checkpoint.progress["attempts"]["n0100-c01-r01"][0]
    assert old_attempt["execution_git_commit"] == "base-commit"
    assert old_attempt["provenance_segment_id"] == "base"
    assert persisted == [True]


def test_resume_rejects_non_monitoring_continuation_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    checkpoint = BatchCheckpoint.__new__(BatchCheckpoint)
    checkpoint.config = _resume_config(
        tmp_path,
        monitoring_only_continuation=True,
    )
    checkpoint.output_dir = tmp_path
    checkpoint.manifest = {
        "git_commit": "base-commit",
        "status": "paused_infrastructure_failure",
        "workload_matrix": {
            "workload_sizes": [100],
            "concurrencies": [1],
            "repetitions": 1,
        },
    }
    checkpoint.progress = {"attempts": {}, "completed_pairs": {}}

    def fake_git(*arguments: str) -> str:
        if arguments == ("status", "--porcelain"):
            return ""
        if arguments == ("rev-parse", "HEAD"):
            return "continuation-commit"
        if arguments[:2] == ("diff", "--name-only"):
            return "architecture-fabric/app/main.py\nbenchmark/resources.py"
        raise AssertionError(arguments)

    monkeypatch.setattr("benchmark.final._git", fake_git)

    with pytest.raises(ValueError, match="unsupported changes"):
        checkpoint._validate_resume()


def test_verify_code_provenance_matches_attempt_commits(
    tmp_path: Path,
) -> None:
    (tmp_path / "source_hashes.json").write_text("{}\n")
    manifest = {
        "git_commit": "base-commit",
        "code_provenance": {
            "base_segment_id": "base",
            "segments": [
                {
                    "segment_id": "base",
                    "git_commit": "base-commit",
                    "change_class": "locked_batch_start",
                    "source_hashes_file": "source_hashes.json",
                }
            ],
        },
    }
    progress = {
        "attempts": {
            "n0100-c01-r01": [
                {
                    "execution_git_commit": "base-commit",
                    "provenance_segment_id": "base",
                }
            ]
        }
    }

    assert _verify_code_provenance(tmp_path, manifest, progress) == 1


def test_fabric_is_primed_but_traditional_is_not(tmp_path: Path) -> None:
    class Driver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, Path]] = []

        def run(
            self,
            action: str,
            architecture: str,
            *,
            log_path: Path,
        ) -> str:
            self.calls.append((action, architecture, log_path))
            return ""

    driver = Driver()
    log_path = tmp_path / "environment.log"

    _prime_if_fabric(  # type: ignore[arg-type]
        driver,
        "traditional",
        log_path=log_path,
    )
    _prime_if_fabric(  # type: ignore[arg-type]
        driver,
        "fabric",
        log_path=log_path,
    )

    assert driver.calls == [("prime", "fabric", log_path)]
