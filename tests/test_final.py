from pathlib import Path

import pytest

from benchmark.final import (
    BatchCheckpoint,
    FinalConfig,
    _namespace,
    _pair_key,
    _prime_if_fabric,
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
