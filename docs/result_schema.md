# Final Result Schema

## Conventions

- CSV files use UTF-8, a header row, comma delimiters, and decimal points.
- UTC timestamps are ISO 8601 strings with an explicit offset.
- Durations ending in `_ms` are milliseconds; `_seconds` fields are seconds.
- Size fields ending in `_bytes` are bytes. CPU and memory percentages are
  numeric percentages, not fractions.
- Boolean values are serialized as `True` or `False` by Python's CSV writer.
- An empty field represents a non-applicable or unavailable nullable value;
  zero remains a measured numeric value.
- `batch_id`, `pair_id`, `attempt_id`, `run_id`, and `request_id` provide the
  provenance hierarchy. Only attempts named by `progress.json` under
  `completed_pairs` enter aggregate result CSVs.

## `requests.csv`

One row per measured HTTP request.

| Field group | Fields and meaning |
|---|---|
| Provenance | `batch_id`, `pair_id`, `repetition`, `attempt_id`, `architecture`, `run_id`, `request_id` |
| Workload | `operation`, `workload_size`, `concurrency`, `request_sequence` |
| Expected result | `expected_http_status`, `expected_authorization_decision` |
| Observed result | `observed_http_status`, `correctness_result`, `outcome_classification` |
| Timing | `latency_ms` for correct outcomes; `time_to_failure_ms` for failures; `started_at_utc` |
| Failure evidence | `error_type`, redacted `error_message`, `late_status` |
| Fabric receipt | `fabric_transaction_id`, `fabric_commit_validation_status`, `fabric_block_number` |

`outcome_classification` is one of `correct_operation_outcome`,
`incorrect_application_result`, `backend_or_platform_failure`, or
`benchmark_infrastructure_failure`. The runner does not retry a measured
request.

## `runs.csv`

One row per architecture, operation, workload size, concurrency, repetition,
and valid attempt. Provenance and workload fields are followed by:

| Field | Definition |
|---|---|
| `attempted_requests` | Requests assigned to the run |
| `response_count` | HTTP responses received inside the boundary |
| `successful_requests` | Correct contract outcomes |
| `failed_requests` | Attempted minus successful |
| `success_rate` | Successful divided by attempted |
| `wall_duration_seconds` | Monotonic duration of the complete operation run |
| `successful_throughput_ops_s` | Correct outcomes divided by wall duration |
| `completed_throughput_ops_s` | HTTP responses divided by wall duration |
| `mean_latency_ms`, `latency_sd_ms` | Successful-request mean and sample standard deviation |
| `p50_latency_ms`, `p95_latency_ms`, `p99_latency_ms` | Type-7 interpolated successful-request quantiles |

## `resources.csv`

One row per sampled container or managed HTTP API process. `sample_phase` is
`idle_baseline` or `measured`. `component_kind` is `container` or `process`;
`component_available=False` records an architecture component that exited
without converting its failure into benchmark invalidity. `container_id` and
`container_name` identify either the container or the `process:<pid>` API
component. CPU, memory, network, block-I/O, and process-count fields retain the
raw numeric Docker/process observations. Network and block-I/O values are zero
for host processes because those counters are unavailable at that boundary.

## `storage.csv`

One row per persistent component plus `architecture_total` after measured-state
validation. `storage_kind` distinguishes PostgreSQL, Fabric ledger/state, and
the summed architecture footprint. `component_available` and `error_message`
retain architecture component failures; missing storage is not silently
reported as a successful zero-byte measurement.

## `correctness.csv`

One untimed durable-state check per resource for OP2, OP5, and OP6. The file
records the expected and observed postconditions, correctness, HTTP status,
and redacted validation failure. Checks cover exact payload identity,
authoritative/observed SHA-256 equality, and the ordered OP1-OP3-OP4 audit
history with expected actors and access decision.

## Fabric block files

- `block_boundaries.csv` stores Fabric channel height immediately before and
  after every operation run, plus the minimum/maximum receipt block and counts
  of observed VALID transactions and distinct blocks. Traditional rows retain
  empty height fields so the run matrix remains rectangular.
- `fabric_blocks.csv` groups successful measured OP1, OP3, and OP4 receipts by
  their returned block number. Counts represent observed benchmark
  transactions, not every envelope that may exist in the block.

## Provenance and recovery files

- `inputs.csv` records each valid pair's deterministic seed, namespace, record
  count, schema version, and aggregate SHA-256 dataset identifier.
- `exclusions.csv` retains every interrupted or technically invalid paired
  attempt and its reason. It is never used to erase the attempt directory.
- `tamper_trials.csv` is reserved for separately executed scenario trials and
  is not populated by the ordinary performance matrix.
- `manifest.json` records the frozen matrix, commit, protocol/dependency hashes,
  allowlisted non-secret configuration, host and Docker metadata, live Fabric
  block-cutting configuration, timing bounds, and architecture order.
- `progress.json` is the resumable checkpoint ledger. On finalization it lists
  exactly one completed attempt per pair.
- `source_hashes.json` maps tracked source paths to SHA-256 digests.
- `verification.json` records structural counts and the durable correctness
  gate.
- `SHA256SUMS` covers every preserved batch file except itself. Final batch
  files and directories are made read-only after verification.
