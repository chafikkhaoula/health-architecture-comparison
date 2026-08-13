"""Reproducible analysis for the SCA'26 architecture comparison.

The independent unit is one fully reset paired repetition.  This module reads
the frozen run-level RQ1 results and the separate RQ2 scenario trials, validates
their structure, computes the prespecified statistics, and writes compact
processed artifacts suitable for a public replication package.

Example
-------
python -m benchmark.analysis \
  --rq1-dir results/raw/final-20260805T122631Z-11a3fbb \
  --rq2-dir results/raw/rq2-tamper/tamper-final-20260809T195823Z-3f4eeaa \
  --output-dir results/processed/sca26
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy
from scipy.stats import rankdata


OPERATIONS = ("OP1", "OP2", "OP3", "OP4", "OP5", "OP6")
WRITE_OPERATIONS = frozenset(("OP1", "OP3", "OP4"))
OUTCOMES = {
    "p95_latency_ms": "P95 latency",
    "successful_throughput_ops_s": "Successful throughput",
}
PAIR_KEYS = ("pair_id", "repetition", "operation", "workload_size", "concurrency")
CONFIG_KEYS = ("operation", "workload_size", "concurrency")
ALPHA = 0.05


class AnalysisError(RuntimeError):
    """Raised when a frozen input violates an analysis invariant."""


@dataclass(frozen=True)
class WilcoxonResult:
    statistic: float
    p_value: float
    w_plus: float
    w_minus: float
    rank_biserial: float
    nonzero_pairs: int


def _require_columns(frame: pd.DataFrame, columns: Iterable[str], source: Path) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise AnalysisError(f"{source} is missing required columns: {missing}")


def _as_bool(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series):
        return series.fillna(False)
    true_values = {"1", "true", "t", "yes", "y"}
    return series.fillna("").astype(str).str.strip().str.lower().isin(true_values)


def _quantile(values: Sequence[float], probability: float) -> float:
    """Type-7 sample quantile, matching NumPy/Pandas method='linear'."""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return math.nan
    return float(np.quantile(array, probability, method="linear"))


def _median(values: Sequence[float]) -> float:
    array = np.asarray(values, dtype=float)
    return float(np.median(array)) if array.size else math.nan


def _derived_seed(base_seed: int, *parts: object) -> int:
    payload = "|".join((str(base_seed), *(str(part) for part in parts)))
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _bootstrap_median_interval(
    values: Sequence[float], resamples: int, seed: int
) -> tuple[float, float]:
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return math.nan, math.nan
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, array.size, size=(resamples, array.size))
    statistics = np.median(array[indices], axis=1)
    return (
        float(np.quantile(statistics, 0.025, method="linear")),
        float(np.quantile(statistics, 0.975, method="linear")),
    )


def exact_pratt_wilcoxon(differences: Sequence[float]) -> WilcoxonResult:
    """Two-sided exhaustive signed-rank permutation test with Pratt zeros.

    Absolute differences, including zeros, receive average ranks.  Zero ranks
    contribute to neither signed-rank sum.  Every sign assignment for the
    non-zero ranks is enumerated, which is deterministic and tractable here
    because each configuration has ten paired repetitions.
    """
    values = np.asarray(differences, dtype=float)
    if values.size == 0 or np.isnan(values).any():
        raise AnalysisError("Wilcoxon input must be non-empty and finite")

    ranks = rankdata(np.abs(values), method="average")
    nonzero = values != 0
    signed_ranks = ranks[nonzero]
    w_plus = float(ranks[values > 0].sum())
    w_minus = float(ranks[values < 0].sum())
    statistic = min(w_plus, w_minus)
    denominator = w_plus + w_minus
    rank_biserial = (w_plus - w_minus) / denominator if denominator else 0.0

    if signed_ranks.size == 0:
        return WilcoxonResult(0.0, 1.0, 0.0, 0.0, 0.0, 0)
    if signed_ranks.size > 20:
        raise AnalysisError(
            "Exhaustive signed-rank enumeration is limited to 20 non-zero pairs"
        )

    extreme = 0
    assignments = 1 << signed_ranks.size
    for signs in product((-1, 1), repeat=signed_ranks.size):
        signs_array = np.asarray(signs)
        permuted_plus = float(signed_ranks[signs_array > 0].sum())
        permuted_minus = float(signed_ranks[signs_array < 0].sum())
        if min(permuted_plus, permuted_minus) <= statistic + 1e-12:
            extreme += 1

    return WilcoxonResult(
        statistic=statistic,
        p_value=extreme / assignments,
        w_plus=w_plus,
        w_minus=w_minus,
        rank_biserial=float(rank_biserial),
        nonzero_pairs=int(signed_ranks.size),
    )


def holm_adjust(p_values: Sequence[float]) -> np.ndarray:
    """Return Holm family-wise-error-adjusted p-values."""
    values = np.asarray(p_values, dtype=float)
    if values.size == 0:
        return values
    if np.isnan(values).any() or ((values < 0) | (values > 1)).any():
        raise AnalysisError("Holm adjustment requires finite p-values in [0, 1]")

    order = np.argsort(values, kind="stable")
    adjusted = np.empty(values.size, dtype=float)
    running_maximum = 0.0
    for rank, original_index in enumerate(order):
        candidate = (values.size - rank) * values[original_index]
        running_maximum = max(running_maximum, candidate)
        adjusted[original_index] = min(1.0, running_maximum)
    return adjusted


def _load_rq1_runs(rq1_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = rq1_dir / "runs.csv"
    runs = pd.read_csv(source)
    required = set(PAIR_KEYS) | {
        "batch_id",
        "attempt_id",
        "architecture",
        "run_id",
        "attempted_requests",
        "successful_requests",
        "failed_requests",
        "success_rate",
        "p95_latency_ms",
        "successful_throughput_ops_s",
    }
    _require_columns(runs, required, source)

    if len(runs) != 1080:
        raise AnalysisError(f"Expected 1,080 run rows, observed {len(runs)}")
    if set(runs["architecture"].unique()) != {"traditional", "fabric"}:
        raise AnalysisError("RQ1 runs must contain exactly Traditional and Fabric")
    if set(runs["operation"].unique()) != set(OPERATIONS):
        raise AnalysisError("RQ1 operation set differs from OP1--OP6")
    if runs.duplicated([*PAIR_KEYS, "architecture"]).any():
        raise AnalysisError("Duplicate architecture member within a paired run")

    counts = runs.groupby([*CONFIG_KEYS, "architecture"], observed=True).size()
    if not (counts == 10).all():
        raise AnalysisError("Every operation/configuration/architecture cell needs 10 runs")

    requested_pairs = runs.pivot(
        index=list(PAIR_KEYS),
        columns="architecture",
        values=[
            "attempt_id",
            "run_id",
            "attempted_requests",
            "successful_requests",
            "failed_requests",
            "success_rate",
            *OUTCOMES,
        ],
    )
    if requested_pairs.isna().any().any():
        raise AnalysisError("At least one RQ1 pair lacks an architecture member")
    requested_pairs.columns = [f"{metric}_{architecture}" for metric, architecture in requested_pairs.columns]
    pairs = requested_pairs.reset_index()

    for outcome in OUTCOMES:
        traditional = pd.to_numeric(pairs[f"{outcome}_traditional"], errors="raise")
        fabric = pd.to_numeric(pairs[f"{outcome}_fabric"], errors="raise")
        if (traditional <= 0).any() or (fabric <= 0).any():
            raise AnalysisError(f"{outcome} must be strictly positive for ratios")
        pairs[f"{outcome}_difference"] = fabric - traditional
        pairs[f"{outcome}_ratio"] = fabric / traditional

    return runs, pairs


def _rq1_statistics(
    pairs: pd.DataFrame, bootstrap_resamples: int, bootstrap_seed: int
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for outcome, outcome_label in OUTCOMES.items():
        for (operation, workload_size, concurrency), group in pairs.groupby(
            list(CONFIG_KEYS), sort=True, observed=True
        ):
            traditional = group[f"{outcome}_traditional"].to_numpy(dtype=float)
            fabric = group[f"{outcome}_fabric"].to_numpy(dtype=float)
            differences = group[f"{outcome}_difference"].to_numpy(dtype=float)
            ratios = group[f"{outcome}_ratio"].to_numpy(dtype=float)
            test = exact_pratt_wilcoxon(differences)
            difference_ci = _bootstrap_median_interval(
                differences,
                bootstrap_resamples,
                _derived_seed(
                    bootstrap_seed, outcome, operation, workload_size, concurrency, "difference"
                ),
            )
            ratio_ci = _bootstrap_median_interval(
                ratios,
                bootstrap_resamples,
                _derived_seed(
                    bootstrap_seed, outcome, operation, workload_size, concurrency, "ratio"
                ),
            )
            records.append(
                {
                    "outcome": outcome,
                    "outcome_label": outcome_label,
                    "operation": operation,
                    "operation_class": (
                        "ledger_write" if operation in WRITE_OPERATIONS else "read_check"
                    ),
                    "workload_size": int(workload_size),
                    "concurrency": int(concurrency),
                    "valid_pairs": int(len(group)),
                    "traditional_median": _median(traditional),
                    "traditional_q1": _quantile(traditional, 0.25),
                    "traditional_q3": _quantile(traditional, 0.75),
                    "fabric_median": _median(fabric),
                    "fabric_q1": _quantile(fabric, 0.25),
                    "fabric_q3": _quantile(fabric, 0.75),
                    "paired_median_difference": _median(differences),
                    "difference_ci95_low": difference_ci[0],
                    "difference_ci95_high": difference_ci[1],
                    "paired_median_ratio": _median(ratios),
                    "ratio_ci95_low": ratio_ci[0],
                    "ratio_ci95_high": ratio_ci[1],
                    "wilcoxon_statistic": test.statistic,
                    "wilcoxon_w_plus": test.w_plus,
                    "wilcoxon_w_minus": test.w_minus,
                    "wilcoxon_nonzero_pairs": test.nonzero_pairs,
                    "p_value_unadjusted": test.p_value,
                    "rank_biserial_fabric_minus_traditional": test.rank_biserial,
                }
            )

    statistics = pd.DataFrame.from_records(records)
    statistics["p_value_holm"] = math.nan
    for (_, _), indices in statistics.groupby(
        ["outcome", "operation"], sort=False, observed=True
    ).groups.items():
        index = list(indices)
        statistics.loc[index, "p_value_holm"] = holm_adjust(
            statistics.loc[index, "p_value_unadjusted"].to_numpy(dtype=float)
        )
    statistics["holm_significant_0_05"] = statistics["p_value_holm"] < ALPHA
    return statistics.sort_values(
        ["outcome", "operation", "workload_size", "concurrency"]
    ).reset_index(drop=True)


def _operation_summary(statistics: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for (outcome, operation), group in statistics.groupby(
        ["outcome", "operation"], sort=True, observed=True
    ):
        configuration_ratios = group["paired_median_ratio"].to_numpy(dtype=float)
        median_ratio = _median(configuration_ratios)
        records.append(
            {
                "outcome": outcome,
                "operation": operation,
                "operation_class": (
                    "ledger_write" if operation in WRITE_OPERATIONS else "read_check"
                ),
                "configuration_count": int(len(group)),
                "operation_level_median_ratio": median_ratio,
                "minimum_configuration_median_ratio": float(configuration_ratios.min()),
                "maximum_configuration_median_ratio": float(configuration_ratios.max()),
                "throughput_retained_percent": (
                    median_ratio * 100
                    if outcome == "successful_throughput_ops_s"
                    else math.nan
                ),
                "holm_significant_configurations": int(
                    group["holm_significant_0_05"].sum()
                ),
            }
        )
    return pd.DataFrame.from_records(records).sort_values(
        ["outcome", "operation"]
    ).reset_index(drop=True)


def _storage_analysis(
    rq1_dir: Path, bootstrap_resamples: int, bootstrap_seed: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = rq1_dir / "storage.csv"
    storage = pd.read_csv(source)
    _require_columns(
        storage,
        {
            "pair_id",
            "repetition",
            "architecture",
            "workload_size",
            "concurrency",
            "component",
            "bytes_used",
            "component_available",
        },
        source,
    )
    totals = storage.loc[storage["component"] == "architecture_total"].copy()
    totals["component_available"] = _as_bool(totals["component_available"])
    if not totals["component_available"].all():
        raise AnalysisError("An architecture-total storage measurement is unavailable")
    if totals.duplicated(["pair_id", "repetition", "architecture"]).any():
        raise AnalysisError("Duplicate architecture-total storage row")

    paired = totals.pivot(
        index=["pair_id", "repetition", "workload_size", "concurrency"],
        columns="architecture",
        values="bytes_used",
    ).reset_index()
    if paired[["traditional", "fabric"]].isna().any().any():
        raise AnalysisError("Storage data contain an incomplete architecture pair")
    paired = paired.rename(
        columns={"traditional": "traditional_bytes", "fabric": "fabric_bytes"}
    )
    paired["fabric_traditional_ratio"] = (
        paired["fabric_bytes"] / paired["traditional_bytes"]
    )

    records: list[dict[str, object]] = []
    for (workload_size, concurrency), group in paired.groupby(
        ["workload_size", "concurrency"], sort=True, observed=True
    ):
        ratios = group["fabric_traditional_ratio"].to_numpy(dtype=float)
        interval = _bootstrap_median_interval(
            ratios,
            bootstrap_resamples,
            _derived_seed(bootstrap_seed, "storage", workload_size, concurrency),
        )
        records.append(
            {
                "workload_size": int(workload_size),
                "concurrency": int(concurrency),
                "valid_pairs": int(len(group)),
                "traditional_median_bytes": _median(group["traditional_bytes"]),
                "fabric_median_bytes": _median(group["fabric_bytes"]),
                "paired_median_ratio": _median(ratios),
                "ratio_ci95_low": interval[0],
                "ratio_ci95_high": interval[1],
            }
        )
    summary = pd.DataFrame.from_records(records)
    return paired.sort_values(["workload_size", "concurrency", "repetition"]), summary


def _block_summary(rq1_dir: Path) -> pd.DataFrame:
    source = rq1_dir / "fabric_blocks.csv"
    blocks = pd.read_csv(source)
    _require_columns(
        blocks,
        {"operation", "concurrency", "block_number", "observed_valid_transactions"},
        source,
    )
    if not set(blocks["operation"].unique()).issubset(WRITE_OPERATIONS):
        raise AnalysisError("Fabric block observations contain a non-write operation")
    summary = (
        blocks.groupby("concurrency", sort=True, observed=True)[
            "observed_valid_transactions"
        ]
        .agg(observed_blocks="size", minimum="min", median="median", maximum="max", total="sum")
        .reset_index()
    )
    return summary


def _rq2_summary(rq2_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source = rq2_dir / "tamper_trials.csv"
    trials = pd.read_csv(source)
    required = {
        "batch_id",
        "scenario_id",
        "protocol_mapping",
        "architecture",
        "trial",
        "trial_id",
        "baseline_verification_passed",
        "baseline_false_positive",
        "mutation_applied",
        "detected",
        "undetected",
        "expected_outcome_met",
        "verification_latency_ms",
        "protected_write_attempted",
        "protected_write_rejected",
        "protected_state_changed",
        "restoration_verified",
        "gateway_http_status",
        "transaction_id",
        "validation_code",
    }
    _require_columns(trials, required, source)
    if len(trials) != 80 or trials["scenario_id"].nunique() != 8:
        raise AnalysisError("RQ2 requires 80 trials across eight separate scenarios")
    if trials.duplicated("trial_id").any():
        raise AnalysisError("RQ2 trial_id values must be unique")

    boolean_columns = (
        "baseline_verification_passed",
        "baseline_false_positive",
        "mutation_applied",
        "detected",
        "undetected",
        "expected_outcome_met",
        "protected_write_attempted",
        "protected_write_rejected",
        "protected_state_changed",
        "restoration_verified",
    )
    for column in boolean_columns:
        trials[column] = _as_bool(trials[column])

    records: list[dict[str, object]] = []
    for scenario_id, group in trials.groupby("scenario_id", sort=False, observed=True):
        protected_attempts = int(group["protected_write_attempted"].sum())
        detected_trials = int(group["detected"].sum())
        applicable_detection = int(
            (group["detected"] | group["undetected"]).sum()
        )
        records.append(
            {
                "batch_id": group["batch_id"].iloc[0],
                "scenario_id": scenario_id,
                "protocol_mapping": group["protocol_mapping"].iloc[0],
                "architecture": group["architecture"].iloc[0],
                "trials": int(len(group)),
                "baseline_verification_passes": int(
                    group["baseline_verification_passed"].sum()
                ),
                "baseline_false_positives": int(group["baseline_false_positive"].sum()),
                "mutated_trials": int(group["mutation_applied"].sum()),
                "detected_trials": detected_trials if applicable_detection else math.nan,
                "undetected_trials": (
                    int(group["undetected"].sum()) if applicable_detection else math.nan
                ),
                "detection_rate": (
                    detected_trials / applicable_detection if applicable_detection else math.nan
                ),
                "protected_write_attempts": protected_attempts,
                "protected_write_rejections": (
                    int(group["protected_write_rejected"].sum())
                    if protected_attempts
                    else math.nan
                ),
                "protected_write_rejection_rate": (
                    float(group["protected_write_rejected"].sum()) / protected_attempts
                    if protected_attempts
                    else math.nan
                ),
                "protected_state_changes": int(group["protected_state_changed"].sum()),
                "expected_outcome_met_trials": int(group["expected_outcome_met"].sum()),
                "restoration_verified_trials": int(group["restoration_verified"].sum()),
                "median_verification_latency_ms": _median(group["verification_latency_ms"]),
                "p95_verification_latency_ms": _quantile(
                    group["verification_latency_ms"], 0.95
                ),
                "unique_transaction_ids": int(
                    group["transaction_id"].dropna().astype(str).nunique()
                ),
                "gateway_http_statuses": ";".join(
                    sorted(
                        {
                            str(int(value))
                            for value in group["gateway_http_status"].dropna().unique()
                        }
                    )
                ),
                "validation_codes": ";".join(
                    sorted(
                        {
                            str(int(value))
                            for value in group["validation_code"].dropna().unique()
                        }
                    )
                ),
            }
        )
    return trials, pd.DataFrame.from_records(records)


def _resource_sampling_summary(rq1_dir: Path) -> dict[str, object]:
    """Optionally summarize complete measured snapshots from the large resource file."""
    source = rq1_dir / "resources.csv"
    if not source.exists():
        return {"available": False, "reason": "resources.csv was not supplied"}

    aggregates: list[pd.DataFrame] = []
    usecols = ["run_id", "sample_phase", "component_kind", "container_id"]
    for chunk in pd.read_csv(source, usecols=usecols, chunksize=250_000):
        measured = chunk.loc[chunk["sample_phase"] == "measured"]
        if measured.empty:
            continue
        aggregates.append(
            measured.groupby(
                ["run_id", "component_kind", "container_id"], observed=True
            )
            .size()
            .rename("component_samples")
            .reset_index()
        )
    if not aggregates:
        return {"available": True, "measured_runs": 0, "complete_snapshots": 0}

    component_counts = (
        pd.concat(aggregates, ignore_index=True)
        .groupby(["run_id", "component_kind", "container_id"], observed=True)[
            "component_samples"
        ]
        .sum()
    )
    complete_counts = component_counts.groupby("run_id", observed=True).min()
    component_count = component_counts.groupby("run_id", observed=True).size()
    maximum_ordinal = component_counts.groupby("run_id", observed=True).max()
    partial_counts = maximum_ordinal - complete_counts
    counts = complete_counts.astype(int)
    one_snapshot = int((counts == 1).sum())
    return {
        "available": True,
        "measured_runs": int(len(counts)),
        "complete_snapshots": int(counts.sum()),
        "minimum_components_per_run": int(component_count.min()),
        "maximum_components_per_run": int(component_count.max()),
        "runs_with_zero_complete_snapshots": int((counts == 0).sum()),
        "runs_with_one_complete_snapshot": one_snapshot,
        "runs_with_one_complete_snapshot_percent": 100.0 * one_snapshot / len(counts),
        "runs_with_two_or_more_complete_snapshots": int((counts >= 2).sum()),
        "partial_snapshot_ordinals_dropped": int(partial_counts.sum()),
        "definition": (
            "Within each measured run, rows are ordered independently for each "
            "component and assigned a zero-based sample ordinal. An ordinal is "
            "complete when every component emitted a row at that ordinal; therefore "
            "the number of complete snapshots is the minimum component sample count."
        ),
    }


def _parse_expected_hashes(checksum_file: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    if not checksum_file.exists():
        return expected
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, relative_path = line.split(maxsplit=1)
        expected[relative_path.strip().lstrip("*")] = digest
    return expected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_selected_source_hashes(directory: Path, names: Sequence[str]) -> dict[str, object]:
    expected = _parse_expected_hashes(directory / "SHA256SUMS")
    results: dict[str, object] = {}
    for name in names:
        path = directory / name
        expected_digest = expected.get(name)
        observed_digest = _sha256(path) if path.exists() else None
        results[name] = {
            "present": path.exists(),
            "expected_sha256": expected_digest,
            "observed_sha256": observed_digest,
            "matches_manifest": (
                observed_digest == expected_digest if expected_digest is not None else None
            ),
        }
    return results


def _plot_operation_summary(summary: pd.DataFrame, output_dir: Path) -> None:
    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    order = ("OP1", "OP3", "OP4", "OP2", "OP5", "OP6")
    labels = {
        "OP1": "OP1 Create record",
        "OP2": "OP2 Retrieve record",
        "OP3": "OP3 Update authorization",
        "OP4": "OP4 Record access decision",
        "OP5": "OP5 Verify integrity",
        "OP6": "OP6 Retrieve audit history",
    }
    colors = {"ledger_write": "#4472C4", "read_check": "#ED7D31"}
    y_positions = np.arange(len(order))[::-1]

    specifications = (
        (
            "p95_latency_ms",
            "fig2_rq1_latency",
            "Request latency relative to the Traditional architecture",
            "Latency ratio (Fabric / Traditional, log scale)",
            True,
        ),
        (
            "successful_throughput_ops_s",
            "fig3_rq1_throughput",
            "Throughput retained relative to the Traditional architecture",
            "Fabric throughput retained (% of Traditional)",
            False,
        ),
    )
    for outcome, filename, title, xlabel, logarithmic in specifications:
        data = summary.loc[summary["outcome"] == outcome].set_index("operation").loc[list(order)]
        center = data["operation_level_median_ratio"].to_numpy(dtype=float)
        low = data["minimum_configuration_median_ratio"].to_numpy(dtype=float)
        high = data["maximum_configuration_median_ratio"].to_numpy(dtype=float)
        if not logarithmic:
            center, low, high = center * 100, low * 100, high * 100

        fig, ax = plt.subplots(figsize=(16, 9), constrained_layout=True)
        for y, operation, value, minimum, maximum in zip(
            y_positions, order, center, low, high, strict=True
        ):
            operation_class = "ledger_write" if operation in WRITE_OPERATIONS else "read_check"
            ax.errorbar(
                value,
                y,
                xerr=np.array([[value - minimum], [maximum - value]]),
                fmt="o",
                markersize=8,
                color=colors[operation_class],
                ecolor="#5f5f5f",
                elinewidth=1.0,
                capsize=3,
                zorder=3,
            )
            suffix = f"{value:.1f}× latency" if logarithmic else f"{value:.1f}% retained"
            text_x = math.sqrt(minimum * maximum) if logarithmic else (minimum + maximum) / 2
            ax.text(text_x, y + 0.27, f"{labels[operation]} — {suffix}", ha="center", fontsize=11)

        ax.set_yticks(y_positions, [""] * len(order))
        ax.set_ylim(-0.5, len(order) - 0.1)
        ax.set_title(title, fontsize=20, pad=32)
        ax.set_xlabel(xlabel, fontsize=12)
        if logarithmic:
            ax.set_xscale("log")
            ax.set_xlim(1, max(200, high.max() * 1.08))
        else:
            ax.set_xlim(0, 100)
        ax.grid(axis="both", color="#d9d9d9", linewidth=0.8)
        ax.set_axisbelow(True)
        for operation_class, legend_label in (
            ("ledger_write", "Write operations"),
            ("read_check", "Read/check operations"),
        ):
            ax.scatter([], [], color=colors[operation_class], s=55, label=legend_label)
        ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False, fontsize=11)
        for extension in ("png", "pdf"):
            fig.savefig(
                figure_dir / f"{filename}.{extension}",
                dpi=300 if extension == "png" else None,
                facecolor="white",
            )
        plt.close(fig)


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, lineterminator="\n", float_format="%.12g")


def _build_validation_report(
    rq1_dir: Path,
    rq2_dir: Path,
    runs: pd.DataFrame,
    pairs: pd.DataFrame,
    statistics: pd.DataFrame,
    operation_summary: pd.DataFrame,
    storage_summary: pd.DataFrame,
    block_summary: pd.DataFrame,
    rq2_trials: pd.DataFrame,
    rq2_summary: pd.DataFrame,
    resource_summary: dict[str, object],
    bootstrap_resamples: int,
    bootstrap_seed: int,
) -> dict[str, object]:
    latency = statistics.loc[statistics["outcome"] == "p95_latency_ms"]
    throughput = statistics.loc[
        statistics["outcome"] == "successful_throughput_ops_s"
    ]
    operation_map = {
        (row.outcome, row.operation): row
        for row in operation_summary.itertuples(index=False)
    }
    rq2_map = {row.scenario_id: row for row in rq2_summary.itertuples(index=False)}

    op6_histories = {
        architecture: int(
            runs.loc[
                (runs["architecture"] == architecture) & (runs["operation"] == "OP6"),
                "attempted_requests",
            ].sum()
        )
        for architecture in ("traditional", "fabric")
    }
    expected_audit_events = {
        architecture: histories * 3 for architecture, histories in op6_histories.items()
    }

    claims = {
        "measured_requests": int(runs["attempted_requests"].sum()),
        "paired_repetitions": int(runs["pair_id"].nunique()),
        "architecture_operation_runs": int(len(runs)),
        "operation_configuration_cells_per_outcome": int(len(latency)),
        "all_run_success_rates_100_percent": bool((runs["success_rate"] == 1.0).all()),
        "fabric_higher_p95_latency_in_all_54_cells": bool(
            (latency["paired_median_ratio"] > 1).all()
        ),
        "fabric_lower_throughput_in_all_54_cells": bool(
            (throughput["paired_median_ratio"] < 1).all()
        ),
        "holm_significant_latency_cells": int(latency["holm_significant_0_05"].sum()),
        "holm_significant_throughput_cells": int(
            throughput["holm_significant_0_05"].sum()
        ),
        "operation_level_latency_ratios": {
            operation: float(
                operation_map[("p95_latency_ms", operation)].operation_level_median_ratio
            )
            for operation in OPERATIONS
        },
        "operation_level_throughput_retained_percent": {
            operation: float(
                operation_map[
                    ("successful_throughput_ops_s", operation)
                ].throughput_retained_percent
            )
            for operation in OPERATIONS
        },
        "storage_configuration_median_ratio_minimum": float(
            storage_summary["paired_median_ratio"].min()
        ),
        "storage_configuration_median_ratio_maximum": float(
            storage_summary["paired_median_ratio"].max()
        ),
        "op6_histories_per_architecture": op6_histories,
        "expected_audit_events_per_architecture": expected_audit_events,
        "fabric_observed_valid_write_transactions": int(block_summary["total"].sum()),
        "maximum_valid_transactions_per_observed_block": int(block_summary["maximum"].max()),
        "rq2_trials": int(len(rq2_trials)),
        "rq2_baseline_false_positives": int(rq2_trials["baseline_false_positive"].sum()),
        "rq2_mutations_applied": int(rq2_trials["mutation_applied"].sum()),
        "rq2_verified_restorations": int(rq2_trials["restoration_verified"].sum()),
        "traditional_partial_changes_detected": int(
            sum(
                rq2_map[scenario].detected_trials
                for scenario in (
                    "T1_PAYLOAD_ONLY",
                    "T2_AUDIT_MODIFY",
                    "T3_AUDIT_DELETE",
                    "T4_AUDIT_REORDER",
                )
            )
        ),
        "traditional_privileged_coherent_rewrites_detected": int(
            sum(
                rq2_map[scenario].detected_trials
                for scenario in (
                    "T5_PRIV_PAYLOAD_HASH_REWRITE",
                    "T6_PRIV_AUDIT_SUFFIX_REWRITE",
                )
            )
        ),
        "fabric_offchain_changes_detected": int(
            rq2_map["F1_OFFCHAIN_PAYLOAD"].detected_trials
        ),
        "fabric_insufficient_endorsements_rejected": int(
            rq2_map["F2_INSUFFICIENT_ENDORSEMENT"].protected_write_rejections
        ),
        "fabric_insufficient_endorsement_validation_codes": (
            rq2_map["F2_INSUFFICIENT_ENDORSEMENT"].validation_codes
        ),
        "fabric_insufficient_endorsement_http_statuses": (
            rq2_map["F2_INSUFFICIENT_ENDORSEMENT"].gateway_http_statuses
        ),
        "fabric_insufficient_endorsement_unique_transaction_ids": int(
            rq2_map["F2_INSUFFICIENT_ENDORSEMENT"].unique_transaction_ids
        ),
    }

    required_claims = {
        "measured_requests": 576_000,
        "paired_repetitions": 90,
        "architecture_operation_runs": 1_080,
        "operation_configuration_cells_per_outcome": 54,
        "all_run_success_rates_100_percent": True,
        "fabric_higher_p95_latency_in_all_54_cells": True,
        "fabric_lower_throughput_in_all_54_cells": True,
        "holm_significant_latency_cells": 46,
        "holm_significant_throughput_cells": 54,
        "rq2_trials": 80,
        "rq2_baseline_false_positives": 0,
        "rq2_mutations_applied": 80,
        "rq2_verified_restorations": 80,
        "traditional_partial_changes_detected": 40,
        "traditional_privileged_coherent_rewrites_detected": 0,
        "fabric_offchain_changes_detected": 10,
        "fabric_insufficient_endorsements_rejected": 10,
        "fabric_insufficient_endorsement_unique_transaction_ids": 10,
        "maximum_valid_transactions_per_observed_block": 10,
    }
    checks = {
        key: {"observed": claims[key], "expected": expected, "pass": claims[key] == expected}
        for key, expected in required_claims.items()
    }
    resource_percent = resource_summary.get("runs_with_one_complete_snapshot_percent")
    if resource_summary.get("available") and resource_percent is not None:
        observed_resource_percent = float(resource_percent)
        claims["runs_with_one_complete_resource_snapshot_percent"] = (
            observed_resource_percent
        )
        checks["runs_with_one_complete_resource_snapshot_percent_rounded"] = {
            "observed": round(observed_resource_percent, 1),
            "expected": 19.3,
            "pass": round(observed_resource_percent, 1) == 19.3,
        }
    report = {
        "status": "PASS" if all(check["pass"] for check in checks.values()) else "FAIL",
        "analysis": {
            "independent_unit": "one fully reset paired repetition",
            "ratio_direction": "Fabric divided by Traditional",
            "difference_direction": "Fabric minus Traditional",
            "wilcoxon": "two-sided exhaustive signed-rank permutation with Pratt zeros",
            "holm_family": "nine workload/concurrency configurations within each outcome-operation family",
            "alpha": ALPHA,
            "bootstrap_resamples": bootstrap_resamples,
            "bootstrap_seed": bootstrap_seed,
            "bootstrap_interval": "2.5th--97.5th percentile of paired median resamples",
            "quantile_method": "type 7 / linear interpolation",
        },
        "software": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scipy": scipy.__version__,
            "matplotlib": matplotlib.__version__,
        },
        "claims": claims,
        "checks": checks,
        "resource_sampling": resource_summary,
        "selected_source_hash_checks": {
            "rq1": _verify_selected_source_hashes(
                rq1_dir,
                (
                    "runs.csv",
                    "storage.csv",
                    "exclusions.csv",
                    "block_boundaries.csv",
                    "fabric_blocks.csv",
                    "inputs.csv",
                    "manifest.json",
                    "verification.json",
                ),
            ),
            "rq2": _verify_selected_source_hashes(
                rq2_dir,
                ("tamper_trials.csv", "scenario_summary.csv", "manifest.json", "verification.json"),
            ),
        },
    }
    return report


def _write_output_checksums(output_dir: Path) -> None:
    files = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [f"{_sha256(path)}  {path.relative_to(output_dir).as_posix()}" for path in files]
    (output_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze(
    rq1_dir: Path,
    rq2_dir: Path,
    output_dir: Path,
    bootstrap_resamples: int = 10_000,
    bootstrap_seed: int = 20_260_813,
    skip_resources: bool = False,
) -> dict[str, object]:
    if bootstrap_resamples < 1:
        raise AnalysisError("bootstrap_resamples must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)

    runs, pairs = _load_rq1_runs(rq1_dir)
    statistics = _rq1_statistics(pairs, bootstrap_resamples, bootstrap_seed)
    operation_summary = _operation_summary(statistics)
    storage_pairs, storage_summary = _storage_analysis(
        rq1_dir, bootstrap_resamples, bootstrap_seed
    )
    block_summary = _block_summary(rq1_dir)
    rq2_trials, rq2_summary = _rq2_summary(rq2_dir)
    exclusions = pd.read_csv(rq1_dir / "exclusions.csv")
    resource_summary = (
        {"available": False, "reason": "resource analysis skipped by command line"}
        if skip_resources
        else _resource_sampling_summary(rq1_dir)
    )

    _write_csv(pairs, output_dir / "rq1_pair_level.csv")
    _write_csv(statistics, output_dir / "rq1_statistics.csv")
    _write_csv(operation_summary, output_dir / "rq1_operation_summary.csv")
    _write_csv(storage_pairs, output_dir / "rq1_storage_pair_level.csv")
    _write_csv(storage_summary, output_dir / "rq1_storage_summary.csv")
    _write_csv(block_summary, output_dir / "rq1_block_summary.csv")
    _write_csv(exclusions, output_dir / "rq1_exclusions.csv")
    _write_csv(rq2_summary, output_dir / "rq2_scenario_summary.csv")
    _plot_operation_summary(operation_summary, output_dir)

    report = _build_validation_report(
        rq1_dir,
        rq2_dir,
        runs,
        pairs,
        statistics,
        operation_summary,
        storage_summary,
        block_summary,
        rq2_trials,
        rq2_summary,
        resource_summary,
        bootstrap_resamples,
        bootstrap_seed,
    )
    (output_dir / "validation_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_output_checksums(output_dir)
    if report["status"] != "PASS":
        raise AnalysisError("One or more locked manuscript claim checks failed")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rq1-dir", type=Path, required=True)
    parser.add_argument("--rq2-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("results/processed/sca26"))
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20_260_813)
    parser.add_argument(
        "--skip-resources",
        action="store_true",
        help="Skip the optional chunked analysis of the large resources.csv file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = analyze(
            rq1_dir=arguments.rq1_dir,
            rq2_dir=arguments.rq2_dir,
            output_dir=arguments.output_dir,
            bootstrap_resamples=arguments.bootstrap_resamples,
            bootstrap_seed=arguments.bootstrap_seed,
            skip_resources=arguments.skip_resources,
        )
    except (AnalysisError, FileNotFoundError, pd.errors.ParserError) as error:
        print(f"analysis failed: {error}", file=sys.stderr)
        return 1
    print(f"analysis status: {report['status']}")
    print(f"processed artifacts: {arguments.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
