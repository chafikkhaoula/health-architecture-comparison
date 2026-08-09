# RQ2 integrity and tamper-evidence execution

This experiment is separate from the frozen RQ1 performance batch
`final-20260805T122631Z-11a3fbb`. The launcher verifies that batch before and
after RQ2, rejects a writable RQ1 tree, and writes RQ2 to a separate directory.

## Scenario cells

Section 12 of `experimental_protocol.md` contains attack variants that must be
reported separately. The executable matrix therefore contains eight scenario
cells rather than collapsing them into five aggregate labels.

| ID | Architecture | Protocol | Independent mutation or attempt |
|---|---|---|---|
| `T1_PAYLOAD_ONLY` | Traditional | 12.1 | Payload changed; database hash unchanged |
| `T2_AUDIT_MODIFY` | Traditional | 12.2a | Audit-event field changed; suffix hashes unchanged |
| `T3_AUDIT_DELETE` | Traditional | 12.2b | Middle audit event deleted; suffix hashes unchanged |
| `T4_AUDIT_REORDER` | Traditional | 12.2c | Adjacent audit sequences swapped; hashes unchanged |
| `T5_PRIV_PAYLOAD_HASH_REWRITE` | Traditional | 12.3a | Payload and database hash rewritten consistently |
| `T6_PRIV_AUDIT_SUFFIX_REWRITE` | Traditional | 12.3b | Audit field and complete affected suffix rehashed |
| `F1_OFFCHAIN_PAYLOAD` | Fabric | 12.1 and 12.4 | Off-chain payload changed; ledger digest unchanged |
| `F2_INSUFFICIENT_ENDORSEMENT` | Fabric | 12.5 | Write restricted to `Org1MSP` under the two-organization policy |

Sections 12.1 and 12.4 map to one Fabric cell because the Fabric PostgreSQL
schema stores only the clinical payload. It has no database-resident copy of
the authoritative digest; that digest is held in Fabric world state. Running
the same payload mutation twice under different labels would create duplicate
evidence, not an independent scenario.

The pilot uses one trial per cell (8 rows). The final experiment uses 10 unique
record identifiers per cell (80 rows). A valid baseline is required before
every mutation. Every database mutation is restored and the baseline verifier
must pass again before the next trial.

## Interpretation rules

- `detected` is scenario-specific. Results are summarized per cell only.
- `expected_outcome_met` is a protocol conformance field, not a reason to erase
  an unexpected scientific result.
- For partial audit attacks, conformance requires detection at the expected
  first broken sequence, not merely detection somewhere in the chain.
- For privileged consistent rewrites, the expected centralized result is no
  internal alarm. The experiment controller retains the original hash or tail
  anchor only to demonstrate what an independent reference could detect.
- For insufficient endorsement, conformance requires an `endorse`-stage
  rejection and identical absent world state before and after the attempt.
- Verification latency covers the verifier only. Mutation, restoration,
  setup, and Fabric submission/rejection time are outside that measurement.

The experiment does not support confidentiality, denial-of-service, absolute
tamper-proofing, or resistance after compromise of every required Fabric
organization. The threat boundary remains the one defined in Section 13 of
the main protocol.

## Artifacts

Each successful batch is made read-only and contains:

- `tamper_trials.csv`: one row per independent trial;
- `scenario_summary.csv`: one summary per scenario cell;
- `manifest.json`: code/protocol/RQ1 provenance and scenario definitions;
- `verification.json`: structural and technical-validity checks;
- `SHA256SUMS`: SHA-256 integrity index for every preserved batch file.

Pilot output is stored under `results/pilot/rq2-tamper/`. Final output is
stored under `results/raw/rq2-tamper/`. Both are separate from the frozen RQ1
batch directory.

## Controlled commands

After the patch is committed on a clean `phase-4-experiment-runner` branch:

```bash
python -m compileall -q shared benchmark tests
python -m pytest -q
git diff --check
git status --short
benchmark/official_tamper.sh pilot
```

The full 10-trial experiment must not be started until the pilot artifacts
have been audited:

```bash
benchmark/official_tamper.sh start
```

## Normative technical basis

- JSON values are canonicalized with RFC 8785 before hashing:
  <https://www.rfc-editor.org/rfc/rfc8785>
- Digests use SHA-256 as specified by NIST FIPS 180-4:
  <https://csrc.nist.gov/pubs/fips/180-4/upd1/final>
- Fabric endorsement policies define which organizations must endorse a
  transaction before it can satisfy chaincode policy:
  <https://hyperledger-fabric.readthedocs.io/en/release-2.5/endorsement-policies.html>
