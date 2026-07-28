# Experimental Protocol

## Study title

Experimental Comparison of Traditional and Permissioned-Blockchain
Architectures for Healthcare Data Exchange

## 1. Objective

This study implements and experimentally compares two healthcare
data-exchange architectures under equivalent workloads:

- Architecture A: FastAPI with PostgreSQL.
- Architecture B: FastAPI with off-chain PostgreSQL and Hyperledger Fabric.

Both architectures will use the same synthetic clinical resources, API
semantics, authorization rules, hashing procedure, workload generator,
and execution environment.

This project is independent of EAL and PrivHealth. It does not implement
EAL-specific consent versioning, typed receipts, IPFS evidence, HAPI FHIR
submission, or audit-timeline reconstruction.

## 2. Research questions

RQ1: How do traditional and permissioned-blockchain architectures differ
in latency, throughput, resource consumption, and storage overhead under
equivalent healthcare data-exchange workloads?

RQ2: How do the architectures differ in audit completeness, integrity
verification, and resistance to privileged history modification?

RQ3: Under which multi-organization healthcare requirements is the
additional overhead of a permissioned blockchain justified?

## 3. Compared architectures

### Architecture A: Traditional

- FastAPI service.
- PostgreSQL clinical payload storage.
- Centralized authorization state.
- SHA-256 record hashes.
- Hash-chained centralized audit log.

### Architecture B: Permissioned blockchain

- FastAPI service implementing the same API contract.
- PostgreSQL off-chain clinical payload storage.
- Two-organization Hyperledger Fabric network.
- On-chain record hashes and non-clinical metadata.
- On-chain authorization state and access events.

No clinical payload will be stored on-chain.

## 4. Experimental data

Only synthetic FHIR R4 resources will be used:

- Patient.
- Observation.
- Condition.
- DiagnosticReport.

No real patient data or personally identifiable information will be used.
The generator will use the fixed random seed 42. Both architectures will
receive byte-equivalent resources with stable identifiers.

## 5. Canonicalization and hashing

Both backends will call the same shared implementation of RFC 8785 JSON
canonicalization and SHA-256 hashing.

The integrity digest is defined as:

payload_hash = SHA-256(canonical_json_bytes)

## 6. Common operations

| ID | Operation | Description |
|---|---|---|
| OP1 | Create record | Store a clinical payload and integrity evidence |
| OP2 | Retrieve record | Retrieve an authorized clinical record |
| OP3 | Update authorization | Create or update an access rule |
| OP4 | Evaluate access | Return allow or deny and record the decision |
| OP5 | Verify integrity | Compare the payload with authoritative evidence |
| OP6 | Retrieve audit | Return the ordered history for a record |

The two backends must expose equivalent request and response semantics.

## 7. Workload design

The candidate final workload matrix is:

| Parameter | Values |
|---|---|
| Measured operations per run | 100, 500, 1000 |
| Concurrent clients | 1, 5, 10 |
| Repetitions | 10 |
| Warm-up | One unmeasured run per configuration |
| Random seed | Fixed and recorded |

A pilot experiment will first validate correctness, estimate execution
time, and identify technical failures. Pilot observations will not be
combined with final measurements.

The final workload matrix will be locked after the pilot and before
collecting final results. It will not be changed in response to the
observed performance of either architecture.

Each paired repetition will use unique identifiers while preserving the
same input resources, authorization rules, and operation order across
the two architectures.

## 8. Timing boundary

Request latency will be measured by the common benchmark client from
immediately before sending the HTTP request until receipt of the complete
HTTP response.

For Fabric write operations, the API will return success only after the
transaction has been validated and its commit event has been confirmed.

Dataset generation, service startup, network deployment, database reset,
warm-up operations, and result serialization are excluded from request
latency.

## 9. Performance metrics

For each architecture, operation, workload size, concurrency level, and
repetition, the study will record:

- successful-request count;
- failed-request count;
- success rate;
- mean latency;
- latency standard deviation;
- median latency (P50);
- P95 latency;
- P99 latency;
- throughput in completed operations per second.

Architecture comparisons will additionally report latency ratios and
throughput differences.

Request-level observations will remain available in raw CSV files.
Statistical comparisons will use independent run-level summaries rather
than treating every request as an independent experimental replicate.

## 10. Resource and storage metrics

The following system-level measurements will be collected:

- average and peak CPU consumption;
- average and peak memory consumption;
- PostgreSQL storage size;
- Fabric ledger and state-database size;
- total architecture storage consumption.

Resource sampling will use the same interval and collection method for
both architectures. Idle baseline consumption will be recorded before
each measured workload.

## 11. Audit completeness

Expected auditable operations are:

- record creation;
- authorization creation or update;
- allowed access;
- denied access.

Audit completeness is calculated as:

audit_completeness =
correctly_recorded_expected_events / expected_auditable_events

An audit event is considered complete only when its identifier, target
record, actor, organization, action, decision, and timestamp match the
corresponding expected event.

## 12. Integrity and tamper-evidence experiments

Each scenario will be executed in 10 independent trials using unique record
identifiers. A valid baseline verification will be performed before every
mutation. Detection outcomes will be reported separately for each scenario.

### 12.1 Off-chain payload-only modification

A stored clinical payload will be modified without updating its integrity
evidence.

Both architectures must detect a mismatch between the recalculated payload
hash and the authoritative stored hash.

### 12.2 Partial centralized audit modification

For the traditional architecture, an audit event will be modified, deleted,
or reordered without recalculating the subsequent audit-chain hashes.

Verification will start from the first event and must identify the first
broken link.

### 12.3 Privileged centralized rewrite

An actor with direct PostgreSQL administration privileges will perform two
separate attacks against the traditional architecture:

- modify a clinical payload and its database-stored hash;
- rewrite an audit-log suffix and recompute the affected audit-chain hashes.

The experiment will determine whether a centralized verifier can distinguish
the resulting internally consistent database state from the original state
without evidence held by an independent authority.

### 12.4 Fabric-backed evidence comparison

For the Fabric architecture, an actor controlling the off-chain PostgreSQL
database will modify a payload and any database-resident copy of its hash.

The corresponding ledger evidence will remain unchanged. Integrity
verification will recalculate the payload hash and compare it with the
committed ledger value.

### 12.5 Insufficient endorsement attempt

A Fabric write requiring endorsement from both participating organizations
will be attempted with an incomplete endorsement set.

The experiment will record:

- gateway or peer response;
- transaction identifier, when available;
- transaction validation outcome, when available;
- world-state value before and after the attempt;
- whether any protected state change occurred.

### 12.6 Tamper-experiment metrics

For each scenario, the study will report:

- baseline false-positive count;
- tampered trials;
- detected trials;
- undetected trials;
- detection rate;
- rejection rate for protected Fabric writes;
- verification latency.

Detection results from different attack scenarios will not be combined into
one aggregate score.

## 13. Threat assumptions and claim boundary

The tamper experiments assume that an attacker may:

- read, modify, or delete data in the off-chain PostgreSQL database;
- invoke an exposed application operation using the identity assigned to the
  test scenario;
- modify a centralized audit history when database privileges permit it.

The attacker is not assumed to:

- compromise the benchmark client or analysis scripts;
- steal the private keys of all required Fabric organizations;
- control the peers of both participating organizations;
- replace the chaincode or endorsement policy;
- compromise the host operating system of every architecture component.

Denial-of-service attacks, traffic-analysis attacks, cryptographic key
recovery, and clinical-data confidentiality are outside the experimental
scope.

The study will describe Fabric evidence as tamper-evident and
policy-governed under these assumptions. It will not describe either
architecture as absolutely tamper-proof.

## 14. Controlled execution procedure

Before each paired repetition, both architectures will start from an
equivalent predefined application state containing the same synthetic
resources and authorization rules.

Warm-up requests will use separate identifiers and will be excluded from all
measurements. Measured requests will begin only after the warm-up state has
been removed or the predefined baseline has been restored.

Execution order will be counterbalanced:

- odd-numbered repetitions: Traditional followed by Fabric;
- even-numbered repetitions: Fabric followed by Traditional.

Within each pair, both architectures will use:

- the same workload seed;
- the same resource contents and identifiers;
- the same logical operation order;
- the same authorization decisions;
- the same benchmark client;
- the same timing boundary;
- the same resource-sampling interval.

Software versions, container-image identifiers, available CPU, available
memory, operating-system information, and background-service state will be
recorded with every experiment batch.

Infrastructure differences inherent to each architecture will be measured
and reported rather than removed from the comparison.

## 15. Statistical analysis plan

The independent experimental unit is one fully reset repetition of a workload
configuration. Individual requests within a repetition are nested observations
and will not be treated as independent experimental replicates.

Traditional and Fabric observations will be paired using the same repetition
number, workload seed, operation, workload size, and concurrency level.

The primary performance outcomes are:

- run-level P95 latency for successful requests;
- run-level successful throughput.

Secondary outcomes are:

- mean latency;
- P50 and P99 latency;
- success and failure rates;
- CPU and memory consumption;
- storage consumption.

For every operation, workload size, and concurrency level, the analysis will
report the ten repetition-level values and their:

- mean and standard deviation;
- median and interquartile range;
- paired architecture difference;
- paired architecture ratio.

Differences are defined as Fabric minus Traditional.

Latency ratios are defined as Fabric divided by Traditional. A value greater
than one therefore indicates higher Fabric latency.

Throughput ratios are defined as Fabric divided by Traditional. A value greater
than one therefore indicates higher Fabric throughput.

The two-sided Wilcoxon signed-rank test will compare the paired run-level
observations. The significance level will be 0.05.

For the primary outcomes, Holm adjustment will control family-wise error
separately within each operation across the nine workload-size and concurrency
configurations. Both unadjusted and adjusted p-values will be retained.

Zero differences will be handled using the Pratt convention. When ties or zero
differences prevent the standard exact calculation, a deterministic exhaustive
permutation calculation will be used.

The analysis will additionally report matched-pairs rank-biserial effect sizes
and 95 percent paired-bootstrap confidence intervals for median differences and
ratios. Bootstrap calculations will use 10000 paired resamples and a fixed,
recorded random seed.

If fewer than eight valid pairs remain for a configuration, no inferential test
will be reported. The available observations and the reason for every missing
pair will still be presented descriptively.

Success and failure outcomes will be reported as counts and run-level rates.
They will not be analysed by treating individual requests as independent
replicates.

Interpretation will emphasize effect magnitude, uncertainty, and operational
relevance rather than statistical significance alone.

Measurements from different operations, configurations, or tamper scenarios
will not be pooled into one aggregate statistical comparison.

## 16. Failure handling, exclusions, and correctness gates

Every measured request will be assigned one of the following outcomes:

- correct operation outcome;
- incorrect application result;
- backend or platform failure;
- benchmark-infrastructure failure.

An expected authorization denial is a correct operation outcome and will not be
classified as a request failure.

A request will be classified as successful only when its response satisfies the
common API contract. The corresponding durable postcondition will be checked
using an untimed validation step after the measured operation or run.

For Fabric writes, successful completion additionally requires a confirmed VALID
commit status. A submitted transaction that is later marked invalid will be
reported as a failed operation, even though it remains visible in the blockchain
history.

The benchmark client will not automatically retry measured HTTP requests.
Any retry behaviour internal to an architecture-specific library or gateway will
be fixed across repetitions and recorded in the experiment metadata.

A client timeout will retain its original timeout outcome. If the final status
of the associated operation can subsequently be determined, it will be stored in
a separate late-status field without rewriting the original client observation.

Failed requests will contribute to failure and success-rate calculations.
Time-to-failure will be retained separately from successful-request latency.
Latency quantiles for successful requests will not be produced when a run
contains no successful requests.

Architecture-specific failures, including service errors, invalid transactions,
or resource exhaustion, are experimental outcomes and will not be used to
invalidate a run.

A repetition may be declared technically invalid only when evidence shows a
failure outside the architecture under test, including:

- an incorrect or incomplete initial reset;
- corrupted or mismatched input data;
- benchmark-client failure;
- resource-monitor failure that makes required measurements unavailable;
- accidental host interruption unrelated to either architecture.

Every invalid repetition and its reason will be retained in an exclusion log.
It will not be silently deleted or replaced.

After correction of a benchmark-infrastructure problem, both members of the
affected Traditional-Fabric pair will be repeated using the same planned seed
and a new attempt identifier. The original invalid artifacts will remain
available.

If one valid member of a pair is unavailable, the pair will be treated as
missing for paired inference. The minimum-valid-pair rule defined in Section 15
will apply.

No observation will be removed solely because its latency or resource value is
large. Suspected anomalies will be investigated against logs and may be excluded
only when they satisfy one of the predefined technical-invalidity conditions.

Before performance inference, every operation must pass its correctness checks,
including response semantics, expected authorization decision, durable state,
integrity verification, and required audit evidence.

A configuration that violates the common correctness contract will be reported
as incorrect and excluded from comparative latency and throughput inference.
Its raw measurements and failure evidence will still be preserved and reported.

Tamper trials will follow their scenario-specific outcomes and will not be used
as ordinary performance repetitions.

## 17. Result artifacts and reproducibility metadata

Each experiment batch will receive an immutable batch identifier. Every paired
repetition will additionally receive a pair identifier, repetition number,
planned seed, and attempt identifier.

Final measurements will be collected from a recorded clean Git commit. The batch
manifest will contain:

- Git commit identifier and repository dirty-state indicator;
- experimental-protocol file hash;
- source-code and dependency-lock-file hashes;
- Docker Engine and Docker Compose versions;
- exact container-image identifiers and digests;
- host operating system, kernel, CPU, available memory, and swap;
- configuration values that affect execution, with secrets removed;
- input-dataset identifier, SHA-256 hash, schema version, and record count;
- workload matrix, random seeds, timeouts, and resource-sampling interval;
- batch start and end times in UTC;
- counterbalanced architecture execution order.

Elapsed durations will be measured using a monotonic high-resolution clock.
UTC timestamps will be stored separately for provenance and event correlation.

The raw result set will include:

- `requests.csv`, containing one row per measured request;
- `runs.csv`, containing one row per completed run;
- `resources.csv`, containing timestamped CPU and memory samples;
- `storage.csv`, containing architecture-specific storage measurements;
- `correctness.csv`, containing expected and observed postconditions;
- `tamper_trials.csv`, containing scenario-specific detection outcomes;
- `exclusions.csv`, containing every invalid repetition and its reason;
- architecture and benchmark logs required to investigate failures;
- `manifest.json`, containing the batch-level metadata;
- `SHA256SUMS`, containing hashes of the preserved batch artifacts.

Request-level records will include, when applicable:

- batch, pair, repetition, attempt, architecture, and request identifiers;
- operation, workload size, concurrency, and request sequence number;
- expected HTTP status and expected authorization decision;
- observed HTTP status, correctness result, and outcome classification;
- latency and time-to-failure;
- error type and redacted error message;
- Fabric transaction identifier and commit-validation status;
- late status observed after a client timeout.

All tabular fields, units, enumerations, and missing-value conventions will be
defined in a version-controlled result schema.

Raw artifacts will become read-only after batch finalization. Corrections or new
analyses will produce versioned derived files without overwriting the original
measurements.

Run-level summaries, statistical results, tables, and figures will be generated
from raw artifacts by version-controlled scripts. Manually transcribed values
will not be used in the paper.

Every reported table and figure will be traceable to its analysis script, input
batch identifiers, input hashes, and software version.

Reproduction instructions will document environment preparation, service
deployment, state reset, pilot execution, final execution, validation, analysis,
and figure generation.

Only synthetic clinical data and non-sensitive identifiers will be retained.
Private keys, access tokens, passwords, and other secrets will not be included
in result archives.

After completion of the final experiments, the protocol, source code, synthetic
data-generation instructions, raw results, analysis scripts, and documentation
will be prepared as a versioned release for deposit in a persistent repository,
subject to publication and institutional requirements.

## Adapter error and authorization semantics

The following semantics are backend-neutral and apply identically to
the Traditional and Fabric adapters:

- OP1 is strict creation. An existing record raises
  `RecordAlreadyExistsError`.
- OP2 requires a matching `ALLOW` rule. A missing rule or explicit
  `DENY` raises `AccessDeniedError`.
- OP2 through OP6 raise `RecordNotFoundError` when the target record
  does not exist.
- OP3 upserts one authorization rule per record and principal tuple.
  The submitted rule identifier becomes the stored identifier.
- OP4 defaults to `DENY` with `matched_rule_id=None` when no rule
  matches and records every ALLOW or DENY decision.
- OP6 returns the ordered protocol events only. Audit-chain
  verification remains internal instrumentation and does not alter
  the frozen OP6 response schema.

## Traditional database privilege and audit-chain scope

PostgreSQL initialization uses a bootstrap administrator that is
separate from the normal application role. The adapter connects only
as a `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE` role with table-level
privileges limited to the operations required by OP1 through OP6.
Privileged database-administrator tampering is therefore evaluated as
a distinct threat scenario rather than as normal application access.

The Traditional adapter deliberately maintains one database-wide
hash-linked audit chain. A transaction-scoped advisory lock serializes
the short audit-append critical section for OP1, OP3, and OP4 across
concurrent records. This serialization is part of the measured
Traditional architecture and must not be removed or replaced by a
per-record chain during the frozen experimental campaign.
