# SCA'26 replication package

This repository contains the two compared implementations, the frozen
experimental protocol, the benchmark runners, and the scripted analysis for
the paper **“With or Without a Permissioned Blockchain? A Controlled
Experimental Study of Security--Performance Trade-offs in Smart Healthcare
Data Exchange.”**

## Frozen experiments

- RQ1 batch: `final-20260805T122631Z-11a3fbb`
- RQ2 batch: `tamper-final-20260809T195823Z-3f4eeaa`
- RQ1 independent unit: one fully reset paired repetition
- RQ1 matrix: 3 workloads × 3 concurrency levels × 10 repetitions × 6 operations
- Measured requests: 576,000
- RQ2: 8 separate scenarios × 10 trials

The complete request- and resource-level RQ1 data are intentionally distributed
as a release asset rather than committed to Git because the uncompressed raw
batch is approximately 4 GB. `sca26-analysis-input-release.zip` contains
the run-level and scenario-level files required to reproduce every primary
statistic, the storage result, the block summary, and Figures 2--3.

## Installation

Create a Python 3.12 environment and install the project requirements plus the
analysis dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-analysis.txt
```

## Reproduce the reported analysis

After extracting the release asset so that it contains `rq1/` and `rq2/`:

```bash
python -m benchmark.analysis \
  --rq1-dir replication-input/rq1 \
  --rq2-dir replication-input/rq2 \
  --output-dir results/processed/sca26 \
  --skip-resources
```

To analyse the full local RQ1 batch, omit `--skip-resources` and point
`--rq1-dir` at the original batch directory. The large `resources.csv` file is
read in chunks.

The command validates the frozen matrix and writes:

- `rq1_pair_level.csv`
- `rq1_statistics.csv`
- `rq1_operation_summary.csv`
- `rq1_storage_pair_level.csv`
- `rq1_storage_summary.csv`
- `rq1_block_summary.csv`
- `rq1_exclusions.csv`
- `rq2_scenario_summary.csv`
- `figures/fig2_rq1_latency.{png,pdf}`
- `figures/fig3_rq1_throughput.{png,pdf}`
- `validation_report.json`
- `SHA256SUMS`

## Statistical specification

For each operation/workload/concurrency cell, the script pairs Fabric and
Traditional run-level observations by repetition. Ratios are Fabric divided by
Traditional; differences are Fabric minus Traditional. It reports medians and
IQRs, paired median ratios and differences, matched-pairs rank-biserial effects,
and fixed-seed 95% paired-bootstrap percentile intervals from 10,000 resamples.

The two-sided Wilcoxon signed-rank calculation ranks zeros using the Pratt
convention and deterministically enumerates all sign assignments of the
non-zero ranked differences. Holm adjustment is applied separately within each
outcome-operation family across its nine workload/concurrency configurations.
Quantiles use type-7 linear interpolation.

## Integrity and privacy

`validation_report.json` records the selected input hashes, library versions,
analysis parameters, and machine-checkable manuscript claims. `SHA256SUMS`
protects every processed output. No `.env` file, private key, generated Fabric
crypto material, database volume, patient information, or credential belongs
in the public repository or release. All workload records are synthetic.
