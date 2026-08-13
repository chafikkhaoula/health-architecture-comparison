# health-architecture-comparison

Reproducible experimental implementation for comparing:

- a Traditional PostgreSQL architecture; and
- a two-organization Hyperledger Fabric architecture with off-chain
  PostgreSQL payload storage.

Both adapters implement the frozen `OP1`-`OP6` contract in `shared/`.
Only synthetic FHIR R4 resources are permitted. The Fabric ledger never
stores a clinical payload.

## Validation

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The PostgreSQL and Fabric integration suites are opt-in because they
require their corresponding deployed infrastructure.

## Architecture-specific setup

- Traditional: `architecture-traditional/`
- Fabric: `architecture-fabric/README.md`
- Frozen experiment design: `docs/experimental_protocol.md`
- Versioned result fields: `docs/result_schema.md`
- Phase 4 runner handoff: `docs/final_runner_handoff.md`
- Paper analysis and replication instructions: `docs/replication_package.md`

## Locked final experiment

After creating both architecture `.env` files and committing a clean source
tree, validate the complete orchestration path once:

```bash
benchmark/final_smoke.sh
```

The smoke batch is stored under `results/pilot/` and is explicitly excluded
from final scientific results. After it prints
`FINAL_ORCHESTRATION_SMOKE_OK`, start the official matrix with:

```bash
benchmark/official_final.sh start
```

The command prints the immutable batch identifier. If the host or runner is
interrupted, resume the same batch without deleting any artifact:

```bash
benchmark/official_final.sh resume FINAL_BATCH_ID
```

Completed Traditional-Fabric pairs are skipped. An interrupted pair is
preserved as an excluded attempt and is repeated in full with a new attempt
identifier. A completed batch can be structurally and cryptographically
verified with:

```bash
benchmark/official_final.sh verify FINAL_BATCH_ID
```

## Reproduce the paper analysis

Download `sca26-analysis-input-release.zip` from the repository's GitHub Release,
extract it as `replication-input/`, and run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-analysis.txt
python -m benchmark.analysis \
  --rq1-dir replication-input/rq1 \
  --rq2-dir replication-input/rq2 \
  --output-dir results/processed/sca26 \
  --skip-resources
```
