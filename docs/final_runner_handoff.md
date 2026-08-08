# Phase 4 Final Runner Handoff

## Locked design

The scaling pilot froze the matrix at `N = 100, 500, 1000`, concurrency
`1, 5, 10`, ten paired repetitions, fixed seed 42, and one unmeasured run per
operation and configuration. The live channel audit established
`BatchTimeout = 2s` and `MaxMessageCount = 10`; the official runner verifies
those values from every fresh Fabric deployment.

The final matrix produces these aggregate structural counts before any
architecture-specific failures are interpreted:

| Artifact | Expected rows |
|---|---:|
| `inputs.csv` | 90 |
| `runs.csv` | 1,080 |
| `requests.csv` | 576,000 |
| `correctness.csv` | 288,000 |
| `block_boundaries.csv` | 1,080 |
| `storage.csv` | 630 |

`resources.csv`, `fabric_blocks.csv`, and `exclusions.csv` have data-dependent
row counts. The verifier checks complete idle/measured resource-phase coverage
for every run rather than accepting a fixed resource-row count.

## Validation sequence

Run from a clean committed worktree:

```bash
.venv/bin/python -m pytest -q

(
  cd architecture-fabric/chaincode
  go test ./...
)

(
  cd architecture-fabric/gateway
  go test ./...
)

bash -n \
  benchmark/services.sh \
  benchmark/final_environment.sh \
  benchmark/final_smoke.sh \
  benchmark/official_final.sh

benchmark/final_smoke.sh
```

Do not start the locked matrix unless the last command prints:

```text
FINAL_ARTIFACTS_VALID
FINAL_ORCHESTRATION_SMOKE_OK
```

## Final execution and recovery

Start once and retain the printed batch identifier:

```bash
benchmark/official_final.sh start
```

If execution is interrupted, do not delete or edit the batch directory. Resume
the same identifier:

```bash
benchmark/official_final.sh resume FINAL_BATCH_ID
```

The checkpoint skips completed pairs. A partial Traditional-Fabric pair remains
in its attempt directory, is appended to `exclusions.csv`, and is repeated in
full under a new attempt identifier. The original raw evidence is never
overwritten.

If an infrastructure-only monitoring defect requires a committed fix after a
batch has started, use the restricted continuation mode:

```bash
benchmark/official_final.sh resume-monitoring FINAL_BATCH_ID
```

This mode accepts only the allowlisted monitoring, orchestration,
documentation, and regression-test paths. It preserves the original batch
commit, records the continuation commit, changed paths, diff hash, and source
hashes in `manifest.json`, and tags every attempt with its execution commit and
provenance segment. Completed pairs are skipped exactly as in an ordinary
resume.

At normal completion the runner prints:

```text
PHASE4_FINAL_COMPLETED_OK
FINAL_ARTIFACTS_VALID
PHASE4_OFFICIAL_FINAL_OK
```

It then makes the finalized batch read-only. `verification.json` reports the
structural checks and the durable correctness gate; raw backend/platform
failures remain in the files even when that gate is not `PASS`.
