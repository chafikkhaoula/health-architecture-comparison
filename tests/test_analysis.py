from pathlib import Path

import pandas as pd
import pytest

from benchmark.analysis import (
    _resource_sampling_summary,
    exact_pratt_wilcoxon,
    holm_adjust,
)


def test_exact_signed_rank_all_positive_ten_pairs() -> None:
    result = exact_pratt_wilcoxon([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert result.statistic == 0
    assert result.p_value == 2 / (2**10)
    assert result.rank_biserial == 1


def test_pratt_zero_is_ranked_but_not_signed() -> None:
    result = exact_pratt_wilcoxon([0, 1, 2])
    assert result.nonzero_pairs == 2
    assert result.w_plus == 5
    assert result.w_minus == 0
    assert result.rank_biserial == 1


def test_holm_adjustment_is_monotone_in_sorted_order() -> None:
    adjusted = holm_adjust([0.01, 0.04, 0.03])
    assert list(adjusted) == pytest.approx([0.03, 0.06, 0.06])


def test_resource_snapshot_completeness_uses_per_component_ordinals(
    tmp_path: Path,
) -> None:
    rows = []
    for run_id, counts in {"run-one": (3, 1), "run-two": (2, 2)}.items():
        for component_index, count in enumerate(counts):
            rows.extend(
                {
                    "run_id": run_id,
                    "sample_phase": "measured",
                    "component_kind": "container",
                    "container_id": f"component-{component_index}",
                }
                for _ in range(count)
            )
    pd.DataFrame(rows).to_csv(tmp_path / "resources.csv", index=False)

    summary = _resource_sampling_summary(tmp_path)

    assert summary["measured_runs"] == 2
    assert summary["complete_snapshots"] == 3
    assert summary["runs_with_one_complete_snapshot"] == 1
    assert summary["runs_with_one_complete_snapshot_percent"] == 50.0
