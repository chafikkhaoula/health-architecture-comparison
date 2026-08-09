from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter_ns
from typing import Any, Sequence

import httpx
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from shared.hashing import payload_sha256
from shared.hashing.audit import (
    StoredAuditLink,
    audit_event_sha256,
    first_invalid_audit_sequence,
)
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuditEvent,
    AuthorizationRule,
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    FHIRResourceEnvelope,
    OperationId,
    RecordLocator,
    ResourceType,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)


ADMINISTRATOR = ActorContext(
    actor_id="administrator-rq2",
    organization_id="org-rq2",
)
PRINCIPAL = ActorContext(
    actor_id="clinician-rq2",
    organization_id="org-rq2",
)


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    architecture: str
    protocol_mapping: str
    mutation_class: str
    verification_method: str
    expected_detection: bool | None
    detection_applicable: bool = True
    protected_write: bool = False


SCENARIOS = (
    ScenarioDefinition(
        "T1_PAYLOAD_ONLY",
        "traditional",
        "12.1",
        "off_chain_payload_only",
        "OP5_database_hash_comparison",
        True,
    ),
    ScenarioDefinition(
        "T2_AUDIT_MODIFY",
        "traditional",
        "12.2a",
        "audit_event_field_modification",
        "global_audit_chain_verifier",
        True,
    ),
    ScenarioDefinition(
        "T3_AUDIT_DELETE",
        "traditional",
        "12.2b",
        "audit_event_deletion",
        "global_audit_chain_verifier",
        True,
    ),
    ScenarioDefinition(
        "T4_AUDIT_REORDER",
        "traditional",
        "12.2c",
        "audit_event_reordering",
        "global_audit_chain_verifier",
        True,
    ),
    ScenarioDefinition(
        "T5_PRIV_PAYLOAD_HASH_REWRITE",
        "traditional",
        "12.3a",
        "privileged_payload_and_hash_rewrite",
        "OP5_database_hash_comparison",
        False,
    ),
    ScenarioDefinition(
        "T6_PRIV_AUDIT_SUFFIX_REWRITE",
        "traditional",
        "12.3b",
        "privileged_audit_suffix_rewrite",
        "global_audit_chain_verifier",
        False,
    ),
    ScenarioDefinition(
        "F1_OFFCHAIN_PAYLOAD",
        "fabric",
        "12.1_and_12.4",
        "off_chain_payload_rewrite_ledger_unchanged",
        "OP5_ledger_hash_comparison",
        True,
    ),
    ScenarioDefinition(
        "F2_INSUFFICIENT_ENDORSEMENT",
        "fabric",
        "12.5",
        "single_organization_endorsement_attempt",
        "gateway_rejection_and_world_state_comparison",
        None,
        detection_applicable=False,
        protected_write=True,
    ),
)
SCENARIO_BY_ID = {item.scenario_id: item for item in SCENARIOS}


@dataclass(frozen=True, slots=True)
class TamperConfig:
    batch_id: str
    rq1_batch_id: str
    traditional_url: str
    fabric_url: str
    fabric_gateway_url: str
    trials: int
    seed: int
    timeout_seconds: float
    output_root: Path
    pilot: bool

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValueError("batch_id must not be empty")
        if not self.rq1_batch_id:
            raise ValueError("rq1_batch_id must not be empty")
        if self.trials < 1:
            raise ValueError("trials must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class TamperTrial:
    batch_id: str
    scenario_id: str
    protocol_mapping: str
    architecture: str
    trial: int
    trial_id: str
    record_type: str
    record_id: str
    mutation_class: str
    verification_method: str
    expected_detection: bool | None
    detection_applicable: bool
    baseline_verification_passed: bool
    baseline_false_positive: bool
    mutation_applied: bool
    detected: bool | None
    undetected: bool | None
    expected_outcome_met: bool
    verification_latency_ms: float
    external_reference_detected: bool | None
    first_invalid_sequence_expected: int | None
    first_invalid_sequence_observed: int | None
    protected_write_attempted: bool
    protected_write_rejected: bool | None
    protected_state_changed: bool | None
    pre_state_present: bool | None
    post_state_present: bool | None
    gateway_http_status: int | None
    gateway_stage: str | None
    transaction_id: str | None
    validation_code: int | None
    restoration_verified: bool
    outcome: str
    started_at_utc: str
    notes: str


@dataclass(frozen=True, slots=True)
class ScenarioSummary:
    batch_id: str
    scenario_id: str
    protocol_mapping: str
    architecture: str
    trials: int
    baseline_false_positives: int
    mutated_trials: int
    detected_trials: int | None
    undetected_trials: int | None
    detection_rate: float | None
    protected_write_attempts: int
    protected_write_rejections: int | None
    protected_write_rejection_rate: float | None
    expected_outcome_met_trials: int
    median_verification_latency_ms: float
    p95_verification_latency_ms: float


AUDIT_COLUMNS = (
    "sequence",
    "event_id",
    "resource_type",
    "resource_id",
    "actor_id",
    "organization_id",
    "action",
    "decision",
    "event_timestamp",
    "previous_hash",
    "event_hash",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _git_value(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _csv_value(value: object) -> object:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return ""
    return value


def _write_dataclasses(path: Path, rows: Sequence[object], row_type: type) -> None:
    names = [item.name for item in fields(row_type)]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: _csv_value(value)
                    for key, value in asdict(row).items()
                }
            )


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * probability
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summaries(
    batch_id: str,
    trials: Sequence[TamperTrial],
) -> tuple[ScenarioSummary, ...]:
    grouped: dict[str, list[TamperTrial]] = defaultdict(list)
    for trial in trials:
        grouped[trial.scenario_id].append(trial)

    summaries = []
    for definition in SCENARIOS:
        rows = grouped.get(definition.scenario_id, [])
        if not rows:
            continue
        latencies = [row.verification_latency_ms for row in rows]
        detection_rows = (
            rows if definition.detection_applicable else []
        )
        protected_rows = (
            rows if definition.protected_write else []
        )
        detected = sum(row.detected is True for row in detection_rows)
        undetected = sum(row.undetected is True for row in detection_rows)
        rejected = sum(
            row.protected_write_rejected is True
            for row in protected_rows
        )
        summaries.append(
            ScenarioSummary(
                batch_id=batch_id,
                scenario_id=definition.scenario_id,
                protocol_mapping=definition.protocol_mapping,
                architecture=definition.architecture,
                trials=len(rows),
                baseline_false_positives=sum(
                    row.baseline_false_positive for row in rows
                ),
                mutated_trials=sum(row.mutation_applied for row in rows),
                detected_trials=(
                    detected if definition.detection_applicable else None
                ),
                undetected_trials=(
                    undetected if definition.detection_applicable else None
                ),
                detection_rate=(
                    detected / len(detection_rows)
                    if detection_rows
                    else None
                ),
                protected_write_attempts=len(protected_rows),
                protected_write_rejections=(
                    rejected if protected_rows else None
                ),
                protected_write_rejection_rate=(
                    rejected / len(protected_rows)
                    if protected_rows
                    else None
                ),
                expected_outcome_met_trials=sum(
                    row.expected_outcome_met for row in rows
                ),
                median_verification_latency_ms=median(latencies),
                p95_verification_latency_ms=_percentile(latencies, 0.95),
            )
        )
    return tuple(summaries)


def _record_id(batch_id: str, scenario_id: str, trial: int) -> str:
    digest = hashlib.sha256(
        f"{batch_id}:{scenario_id}:{trial}".encode("utf-8")
    ).hexdigest()[:14]
    short = scenario_id.split("_", 1)[0].lower()
    return f"rq2-{short}-{trial:02d}-{digest}"


def _resource(record_id: str) -> FHIRResourceEnvelope:
    return FHIRResourceEnvelope.from_payload(
        {
            "resourceType": "Patient",
            "id": record_id,
            "active": True,
            "gender": "unknown",
        }
    )


def _record(record_id: str) -> RecordLocator:
    return RecordLocator(
        resource_type=ResourceType.PATIENT,
        resource_id=record_id,
    )


def _audit_link(row: dict[str, Any]) -> StoredAuditLink:
    return StoredAuditLink(
        event=AuditEvent(
            event_id=row["event_id"],
            sequence=int(row["sequence"]),
            record=RecordLocator(
                resource_type=ResourceType(row["resource_type"]),
                resource_id=row["resource_id"],
            ),
            actor=ActorContext(
                actor_id=row["actor_id"],
                organization_id=row["organization_id"],
            ),
            action=OperationId(row["action"]),
            decision=(
                AccessDecision(row["decision"])
                if row["decision"] is not None
                else None
            ),
            timestamp=row["event_timestamp"],
        ),
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
    )


def _first_invalid_rows(rows: Sequence[dict[str, Any]]) -> int | None:
    return first_invalid_audit_sequence(
        tuple(_audit_link(row) for row in rows)
    )


async def _all_audit_rows(
    connection: AsyncConnection[Any],
) -> list[dict[str, Any]]:
    cursor = await connection.execute(
        f"SELECT {', '.join(AUDIT_COLUMNS)} "
        "FROM audit_events ORDER BY sequence"
    )
    return list(await cursor.fetchall())


async def _record_audit_rows(
    connection: AsyncConnection[Any],
    record_id: str,
) -> list[dict[str, Any]]:
    cursor = await connection.execute(
        f"SELECT {', '.join(AUDIT_COLUMNS)} "
        "FROM audit_events "
        "WHERE resource_type = %s AND resource_id = %s "
        "ORDER BY sequence",
        ("Patient", record_id),
    )
    return list(await cursor.fetchall())


async def _first_invalid(
    connection: AsyncConnection[Any],
) -> int | None:
    return _first_invalid_rows(await _all_audit_rows(connection))


def _database_conninfo(prefix: str, *, application: bool) -> str:
    role = "APP" if application else "ADMIN"
    return make_conninfo(
        host="127.0.0.1",
        port=os.environ[f"RQ2_{prefix}_POSTGRES_PORT"],
        dbname=os.environ[f"RQ2_{prefix}_POSTGRES_DB"],
        user=os.environ[f"RQ2_{prefix}_{role}_USER"],
        password=os.environ[f"RQ2_{prefix}_{role}_PASSWORD"],
    )


async def _post_model(
    client: httpx.AsyncClient,
    operation: OperationId,
    request: object,
    result_type: type,
    expected_status: int,
) -> tuple[Any, httpx.Response, float]:
    started = perf_counter_ns()
    response = await client.post(
        f"/v1/operations/{operation.value}",
        json=request.model_dump(mode="json"),
    )
    elapsed = (perf_counter_ns() - started) / 1_000_000
    if response.status_code != expected_status:
        message = " ".join(response.text.split())[:300]
        raise RuntimeError(
            f"{operation.value} returned {response.status_code}: {message}"
        )
    return (
        result_type.model_validate(response.json(), strict=False),
        response,
        elapsed,
    )


class TamperExperiment:
    def __init__(
        self,
        config: TamperConfig,
        traditional_client: httpx.AsyncClient,
        fabric_client: httpx.AsyncClient,
        gateway_client: httpx.AsyncClient,
        traditional_admin: AsyncConnection[Any],
        fabric_admin: AsyncConnection[Any],
    ) -> None:
        self.config = config
        self.clients = {
            "traditional": traditional_client,
            "fabric": fabric_client,
        }
        self.gateway_client = gateway_client
        self.traditional_admin = traditional_admin
        self.fabric_admin = fabric_admin

    async def _create(self, architecture: str, record_id: str) -> None:
        result, _, _ = await _post_model(
            self.clients[architecture],
            OperationId.CREATE_RECORD,
            CreateRecordRequest(
                resource=_resource(record_id),
                actor=ADMINISTRATOR,
            ),
            CreateRecordResult,
            201,
        )
        if result.record != _record(record_id):
            raise RuntimeError("OP1 returned a mismatched record")

    async def _verify_integrity(
        self,
        architecture: str,
        record_id: str,
    ) -> tuple[VerifyIntegrityResult, float]:
        result, _, elapsed = await _post_model(
            self.clients[architecture],
            OperationId.VERIFY_INTEGRITY,
            VerifyIntegrityRequest(record=_record(record_id)),
            VerifyIntegrityResult,
            200,
        )
        return result, elapsed

    async def _build_audit_history(self, record_id: str) -> None:
        await self._create("traditional", record_id)
        record = _record(record_id)
        for index, decision in enumerate(
            (AccessDecision.ALLOW, AccessDecision.DENY),
            start=1,
        ):
            rule = AuthorizationRule(
                rule_id=f"rule-{record_id}-{index}",
                record=record,
                principal=PRINCIPAL,
                decision=decision,
            )
            updated, _, _ = await _post_model(
                self.clients["traditional"],
                OperationId.UPDATE_AUTHORIZATION,
                UpdateAuthorizationRequest(
                    rule=rule,
                    actor=ADMINISTRATOR,
                ),
                UpdateAuthorizationResult,
                200,
            )
            if updated.rule != rule:
                raise RuntimeError("OP3 returned a mismatched rule")
            evaluated, _, _ = await _post_model(
                self.clients["traditional"],
                OperationId.EVALUATE_ACCESS,
                EvaluateAccessRequest(record=record, actor=PRINCIPAL),
                EvaluateAccessResult,
                200,
            )
            if evaluated.decision is not decision:
                raise RuntimeError("OP4 returned a mismatched decision")

    def _common_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
        record_id: str,
        *,
        detected: bool | None,
        elapsed: float,
        outcome: str,
        restoration_verified: bool,
        external_reference_detected: bool | None = None,
        expected_sequence: int | None = None,
        observed_sequence: int | None = None,
        protected_write_rejected: bool | None = None,
        protected_state_changed: bool | None = None,
        pre_state_present: bool | None = None,
        post_state_present: bool | None = None,
        gateway_http_status: int | None = None,
        gateway_stage: str | None = None,
        transaction_id: str | None = None,
        validation_code: int | None = None,
        notes: str = "",
    ) -> TamperTrial:
        expected_met = (
            protected_write_rejected is True
            and protected_state_changed is False
            and gateway_stage == "endorse"
            if definition.protected_write
            else detected is definition.expected_detection
        )
        if expected_sequence is not None:
            expected_met = (
                expected_met and observed_sequence == expected_sequence
            )
        return TamperTrial(
            batch_id=self.config.batch_id,
            scenario_id=definition.scenario_id,
            protocol_mapping=definition.protocol_mapping,
            architecture=definition.architecture,
            trial=trial,
            trial_id=f"{definition.scenario_id}-trial{trial:02d}",
            record_type="Patient",
            record_id=record_id,
            mutation_class=definition.mutation_class,
            verification_method=definition.verification_method,
            expected_detection=definition.expected_detection,
            detection_applicable=definition.detection_applicable,
            baseline_verification_passed=True,
            baseline_false_positive=False,
            mutation_applied=True,
            detected=detected,
            undetected=(
                not detected
                if definition.detection_applicable and detected is not None
                else None
            ),
            expected_outcome_met=expected_met,
            verification_latency_ms=elapsed,
            external_reference_detected=external_reference_detected,
            first_invalid_sequence_expected=expected_sequence,
            first_invalid_sequence_observed=observed_sequence,
            protected_write_attempted=definition.protected_write,
            protected_write_rejected=protected_write_rejected,
            protected_state_changed=protected_state_changed,
            pre_state_present=pre_state_present,
            post_state_present=post_state_present,
            gateway_http_status=gateway_http_status,
            gateway_stage=gateway_stage,
            transaction_id=transaction_id,
            validation_code=validation_code,
            restoration_verified=restoration_verified,
            outcome=outcome,
            started_at_utc=_utc_now().isoformat(),
            notes=notes,
        )

    async def _payload_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
        *,
        rewrite_hash: bool,
    ) -> TamperTrial:
        architecture = definition.architecture
        connection = (
            self.traditional_admin
            if architecture == "traditional"
            else self.fabric_admin
        )
        table = (
            "clinical_records"
            if architecture == "traditional"
            else "clinical_payloads"
        )
        record_id = _record_id(
            f"{self.config.batch_id}:{self.config.seed}",
            definition.scenario_id,
            trial,
        )
        await self._create(architecture, record_id)
        baseline, _ = await self._verify_integrity(architecture, record_id)
        if not baseline.matches:
            raise RuntimeError("valid OP5 baseline reported a mismatch")

        columns = "payload, payload_hash" if rewrite_hash else "payload"
        cursor = await connection.execute(
            f"SELECT {columns} FROM {table} "
            "WHERE resource_type = %s AND resource_id = %s",
            ("Patient", record_id),
        )
        original = await cursor.fetchone()
        if original is None:
            raise RuntimeError("created payload was not found")
        tampered_payload = dict(original["payload"])
        tampered_payload["active"] = not bool(
            tampered_payload.get("active", False)
        )
        if rewrite_hash:
            tampered_hash = payload_sha256(tampered_payload)
            await connection.execute(
                f"UPDATE {table} SET payload = %s, payload_hash = %s "
                "WHERE resource_type = %s AND resource_id = %s",
                (
                    Jsonb(tampered_payload),
                    tampered_hash,
                    "Patient",
                    record_id,
                ),
            )
        else:
            await connection.execute(
                f"UPDATE {table} SET payload = %s "
                "WHERE resource_type = %s AND resource_id = %s",
                (Jsonb(tampered_payload), "Patient", record_id),
            )
        await connection.commit()

        observed, elapsed = await self._verify_integrity(
            architecture,
            record_id,
        )
        detected = not observed.matches
        external_detected = (
            observed.authoritative_evidence.payload_hash
            != baseline.authoritative_evidence.payload_hash
            or observed.observed_hash != baseline.observed_hash
        )

        if rewrite_hash:
            await connection.execute(
                f"UPDATE {table} SET payload = %s, payload_hash = %s "
                "WHERE resource_type = %s AND resource_id = %s",
                (
                    Jsonb(original["payload"]),
                    original["payload_hash"],
                    "Patient",
                    record_id,
                ),
            )
        else:
            await connection.execute(
                f"UPDATE {table} SET payload = %s "
                "WHERE resource_type = %s AND resource_id = %s",
                (Jsonb(original["payload"]), "Patient", record_id),
            )
        await connection.commit()
        restored, _ = await self._verify_integrity(architecture, record_id)
        restoration_verified = restored.matches
        if not restoration_verified:
            raise RuntimeError("payload restoration verification failed")

        return self._common_trial(
            definition,
            trial,
            record_id,
            detected=detected,
            elapsed=elapsed,
            outcome=("detected" if detected else "internally_consistent"),
            restoration_verified=restoration_verified,
            external_reference_detected=external_detected,
            notes=(
                "database payload and database hash were rewritten"
                if rewrite_hash
                else "authoritative evidence was intentionally unchanged"
            ),
        )

    async def _partial_audit_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
        variant: str,
    ) -> TamperTrial:
        record_id = _record_id(
            f"{self.config.batch_id}:{self.config.seed}",
            definition.scenario_id,
            trial,
        )
        await self._build_audit_history(record_id)
        if await _first_invalid(self.traditional_admin) is not None:
            raise RuntimeError("valid audit baseline contained a broken link")
        target_rows = await _record_audit_rows(
            self.traditional_admin,
            record_id,
        )
        if len(target_rows) != 5:
            raise RuntimeError("audit trial requires exactly five events")

        if variant == "modify":
            target = target_rows[1]
            expected = int(target["sequence"])
            changed_actor = f"{target['actor_id']}.tampered"
            await self.traditional_admin.execute(
                "UPDATE audit_events SET actor_id = %s WHERE sequence = %s",
                (changed_actor, expected),
            )
            await self.traditional_admin.commit()

            started = perf_counter_ns()
            observed = await _first_invalid(self.traditional_admin)
            elapsed = (perf_counter_ns() - started) / 1_000_000

            await self.traditional_admin.execute(
                "UPDATE audit_events SET actor_id = %s WHERE sequence = %s",
                (target["actor_id"], expected),
            )
        elif variant == "delete":
            target = target_rows[2]
            following = target_rows[3]
            expected = int(following["sequence"])
            await self.traditional_admin.execute(
                "DELETE FROM audit_events WHERE sequence = %s",
                (target["sequence"],),
            )
            await self.traditional_admin.commit()

            started = perf_counter_ns()
            observed = await _first_invalid(self.traditional_admin)
            elapsed = (perf_counter_ns() - started) / 1_000_000

            placeholders = ", ".join(["%s"] * len(AUDIT_COLUMNS))
            await self.traditional_admin.execute(
                f"INSERT INTO audit_events ({', '.join(AUDIT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                tuple(target[column] for column in AUDIT_COLUMNS),
            )
        elif variant == "reorder":
            first, second = target_rows[1], target_rows[2]
            first_sequence = int(first["sequence"])
            second_sequence = int(second["sequence"])
            expected = first_sequence
            cursor = await self.traditional_admin.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1000 AS temporary "
                "FROM audit_events"
            )
            temporary = int((await cursor.fetchone())["temporary"])
            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (temporary, first_sequence),
            )
            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (first_sequence, second_sequence),
            )
            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (second_sequence, temporary),
            )
            await self.traditional_admin.commit()

            started = perf_counter_ns()
            observed = await _first_invalid(self.traditional_admin)
            elapsed = (perf_counter_ns() - started) / 1_000_000

            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (temporary, second_sequence),
            )
            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (second_sequence, first_sequence),
            )
            await self.traditional_admin.execute(
                "UPDATE audit_events SET sequence = %s WHERE sequence = %s",
                (first_sequence, temporary),
            )
        else:
            raise ValueError(f"unsupported partial-audit variant: {variant}")

        await self.traditional_admin.commit()
        restoration_verified = (
            await _first_invalid(self.traditional_admin) is None
        )
        if not restoration_verified:
            raise RuntimeError("audit restoration verification failed")
        detected = observed is not None
        return self._common_trial(
            definition,
            trial,
            record_id,
            detected=detected,
            elapsed=elapsed,
            outcome=("detected" if detected else "undetected"),
            restoration_verified=restoration_verified,
            expected_sequence=expected,
            observed_sequence=observed,
            notes=f"partial audit variant={variant}",
        )

    async def _audit_suffix_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
    ) -> TamperTrial:
        record_id = _record_id(
            f"{self.config.batch_id}:{self.config.seed}",
            definition.scenario_id,
            trial,
        )
        await self._build_audit_history(record_id)
        if await _first_invalid(self.traditional_admin) is not None:
            raise RuntimeError("valid audit baseline contained a broken link")
        target_rows = await _record_audit_rows(
            self.traditional_admin,
            record_id,
        )
        target_sequence = int(target_rows[1]["sequence"])
        all_rows = await _all_audit_rows(self.traditional_admin)
        suffix = [
            dict(row)
            for row in all_rows
            if int(row["sequence"]) >= target_sequence
        ]
        original_anchor = all_rows[-1]["event_hash"]

        changed_actor = f"{suffix[0]['actor_id']}.tampered"
        await self.traditional_admin.execute(
            "UPDATE audit_events SET actor_id = %s WHERE sequence = %s",
            (changed_actor, target_sequence),
        )
        suffix[0]["actor_id"] = changed_actor
        prefix = [
            row
            for row in all_rows
            if int(row["sequence"]) < target_sequence
        ]
        previous_hash = prefix[-1]["event_hash"] if prefix else None
        for row in suffix:
            row["previous_hash"] = previous_hash
            link = _audit_link(
                {
                    **row,
                    "event_hash": "0" * 64,
                }
            )
            row["event_hash"] = audit_event_sha256(
                link.event,
                previous_hash,
            )
            previous_hash = row["event_hash"]
            await self.traditional_admin.execute(
                "UPDATE audit_events "
                "SET previous_hash = %s, event_hash = %s "
                "WHERE sequence = %s",
                (
                    row["previous_hash"],
                    row["event_hash"],
                    row["sequence"],
                ),
            )
        await self.traditional_admin.commit()

        started = perf_counter_ns()
        observed = await _first_invalid(self.traditional_admin)
        elapsed = (perf_counter_ns() - started) / 1_000_000
        rewritten_rows = await _all_audit_rows(self.traditional_admin)
        rewritten_anchor = rewritten_rows[-1]["event_hash"]

        for row in suffix:
            original = next(
                item
                for item in all_rows
                if item["sequence"] == row["sequence"]
            )
            await self.traditional_admin.execute(
                "UPDATE audit_events "
                "SET actor_id = %s, previous_hash = %s, event_hash = %s "
                "WHERE sequence = %s",
                (
                    original["actor_id"],
                    original["previous_hash"],
                    original["event_hash"],
                    original["sequence"],
                ),
            )
        await self.traditional_admin.commit()
        restoration_verified = (
            await _first_invalid(self.traditional_admin) is None
        )
        if not restoration_verified:
            raise RuntimeError("audit suffix restoration verification failed")
        detected = observed is not None
        return self._common_trial(
            definition,
            trial,
            record_id,
            detected=detected,
            elapsed=elapsed,
            outcome=("detected" if detected else "internally_consistent"),
            restoration_verified=restoration_verified,
            external_reference_detected=(
                rewritten_anchor != original_anchor
            ),
            observed_sequence=observed,
            notes=(
                "centralized suffix was recomputed; external anchor retained "
                "only by the experiment controller"
            ),
        )

    async def _endorsement_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
    ) -> TamperTrial:
        record_id = _record_id(
            f"{self.config.batch_id}:{self.config.seed}",
            definition.scenario_id,
            trial,
        )
        resource = _resource(record_id)
        query = {
            "function": "GetRecordEvidence",
            "arguments": ["Patient", record_id],
        }
        before = await self.gateway_client.post("/v1/evaluate", json=query)
        pre_present = before.status_code == 200
        if before.status_code != 404:
            raise RuntimeError(
                "unique endorsement key was present before the attempt"
            )

        submit = await self.gateway_client.post(
            "/v1/submit",
            json={
                "function": "CreateRecord",
                "arguments": [
                    "Patient",
                    record_id,
                    payload_sha256(resource.payload),
                    ADMINISTRATOR.actor_id,
                    ADMINISTRATOR.organization_id,
                ],
                "endorsing_organizations": ["Org1MSP"],
            },
        )
        try:
            body = submit.json()
        except json.JSONDecodeError:
            body = {}
        error = body.get("error", {}) if isinstance(body, dict) else {}
        rejected = submit.status_code >= 400

        started = perf_counter_ns()
        after = await self.gateway_client.post("/v1/evaluate", json=query)
        elapsed = (perf_counter_ns() - started) / 1_000_000
        post_present = after.status_code == 200
        if after.status_code not in {200, 404}:
            raise RuntimeError(
                "could not determine post-attempt Fabric world state"
            )
        state_changed = post_present != pre_present
        return self._common_trial(
            definition,
            trial,
            record_id,
            detected=None,
            elapsed=elapsed,
            outcome=(
                "rejected_no_state_change"
                if rejected and not state_changed
                else "protected_state_failure"
            ),
            restoration_verified=True,
            protected_write_rejected=rejected,
            protected_state_changed=state_changed,
            pre_state_present=pre_present,
            post_state_present=post_present,
            gateway_http_status=submit.status_code,
            gateway_stage=(
                str(error.get("stage")) if error.get("stage") else None
            ),
            transaction_id=(
                str(error.get("transaction_id"))
                if error.get("transaction_id")
                else None
            ),
            validation_code=(
                int(error["validation_code"])
                if error.get("validation_code") is not None
                else None
            ),
            notes="endorsement was restricted to Org1MSP",
        )

    async def run_trial(
        self,
        definition: ScenarioDefinition,
        trial: int,
    ) -> TamperTrial:
        if definition.scenario_id == "T1_PAYLOAD_ONLY":
            return await self._payload_trial(
                definition, trial, rewrite_hash=False
            )
        if definition.scenario_id == "T2_AUDIT_MODIFY":
            return await self._partial_audit_trial(
                definition, trial, "modify"
            )
        if definition.scenario_id == "T3_AUDIT_DELETE":
            return await self._partial_audit_trial(
                definition, trial, "delete"
            )
        if definition.scenario_id == "T4_AUDIT_REORDER":
            return await self._partial_audit_trial(
                definition, trial, "reorder"
            )
        if definition.scenario_id == "T5_PRIV_PAYLOAD_HASH_REWRITE":
            return await self._payload_trial(
                definition, trial, rewrite_hash=True
            )
        if definition.scenario_id == "T6_PRIV_AUDIT_SUFFIX_REWRITE":
            return await self._audit_suffix_trial(definition, trial)
        if definition.scenario_id == "F1_OFFCHAIN_PAYLOAD":
            return await self._payload_trial(
                definition, trial, rewrite_hash=False
            )
        if definition.scenario_id == "F2_INSUFFICIENT_ENDORSEMENT":
            return await self._endorsement_trial(definition, trial)
        raise ValueError(f"unsupported scenario: {definition.scenario_id}")


def _manifest(config: TamperConfig, output_dir: Path) -> dict[str, object]:
    protocol = Path("docs/experimental_protocol.md")
    execution_protocol = Path("docs/rq2_tamper_execution.md")
    rq1_dir = Path("results/raw") / config.rq1_batch_id
    dirty = _git_value("status", "--porcelain")
    return {
        "schema_version": 1,
        "experiment": "rq2_integrity_and_tamper_evidence",
        "pilot": config.pilot,
        "batch_id": config.batch_id,
        "status": "running",
        "started_at_utc": _utc_now().isoformat(),
        "ended_at_utc": None,
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_dirty": dirty is None or bool(dirty),
        "git_branch": _git_value("branch", "--show-current"),
        "protocol_sha256": _sha256(protocol),
        "execution_protocol_sha256": _sha256(execution_protocol),
        "runner_sha256": _sha256(Path("benchmark/tamper.py")),
        "launcher_sha256": _sha256(
            Path("benchmark/official_tamper.sh")
        ),
        "requirements_sha256": _sha256(Path("requirements.txt")),
        "rq1_reference": {
            "batch_id": config.rq1_batch_id,
            "path": str(rq1_dir),
            "sha256sums_file_sha256": _sha256(rq1_dir / "SHA256SUMS"),
            "read_only_guard": True,
        },
        "trials_per_scenario": config.trials,
        "seed": config.seed,
        "timeout_seconds": config.timeout_seconds,
        "scenario_count": len(SCENARIOS),
        "expected_trial_count": len(SCENARIOS) * config.trials,
        "scenario_definitions": [asdict(item) for item in SCENARIOS],
        "scenario_aggregation": "separate_only",
        "result_files": {
            "trials": str(output_dir / "tamper_trials.csv"),
            "summaries": str(output_dir / "scenario_summary.csv"),
        },
    }


def _persist(
    output_dir: Path,
    manifest: dict[str, object],
    trials: Sequence[TamperTrial],
) -> None:
    _write_dataclasses(
        output_dir / "tamper_trials.csv", trials, TamperTrial
    )
    if trials:
        available = {row.scenario_id for row in trials}
        summaries = tuple(
            item
            for item in _summaries(manifest["batch_id"], trials)
            if item.scenario_id in available
        )
    else:
        summaries = ()
    _write_dataclasses(
        output_dir / "scenario_summary.csv",
        summaries,
        ScenarioSummary,
    )
    _write_json(output_dir / "manifest.json", manifest)


def _write_hashes(output_dir: Path) -> None:
    lines = []
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(
                f"{_sha256(path)}  {path.relative_to(output_dir)}"
            )
    (output_dir / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _freeze(output_dir: Path) -> None:
    for path in sorted(output_dir.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    output_dir.chmod(0o555)


def _read_bool(value: str) -> bool:
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    raise ValueError(f"invalid boolean: {value}")


def verify_output(output_dir: Path) -> dict[str, object]:
    manifest = json.loads(
        (output_dir / "manifest.json").read_text(encoding="utf-8")
    )
    if manifest.get("status") != "completed":
        raise ValueError("tamper batch is not completed")
    if manifest.get("git_dirty") is not False:
        raise ValueError("tamper batch was not executed from a clean tree")
    expected_definitions = [asdict(item) for item in SCENARIOS]
    if manifest.get("scenario_definitions") != expected_definitions:
        raise ValueError("scenario definitions do not match the runner")
    rq1_reference = manifest.get("rq1_reference")
    if not isinstance(rq1_reference, dict):
        raise ValueError("RQ1 reference provenance is missing")
    rq1_checksum_path = (
        Path(str(rq1_reference.get("path", ""))) / "SHA256SUMS"
    )
    if (
        not rq1_checksum_path.is_file()
        or _sha256(rq1_checksum_path)
        != rq1_reference.get("sha256sums_file_sha256")
    ):
        raise ValueError("frozen RQ1 reference checksum changed")
    expected_per_scenario = int(manifest["trials_per_scenario"])
    expected_total = len(SCENARIOS) * expected_per_scenario
    with (output_dir / "tamper_trials.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != expected_total:
        raise ValueError("tamper trial cardinality is incorrect")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["scenario_id"]].append(row)
    if set(grouped) != set(SCENARIO_BY_ID):
        raise ValueError("tamper scenario coverage is incorrect")
    if any(
        len(values) != expected_per_scenario
        for values in grouped.values()
    ):
        raise ValueError("per-scenario trial cardinality is incorrect")
    if len({row["trial_id"] for row in rows}) != expected_total:
        raise ValueError("trial identifiers are not unique")
    if len({row["record_id"] for row in rows}) != expected_total:
        raise ValueError("record identifiers are not unique")
    if any(
        not _read_bool(row["baseline_verification_passed"])
        or _read_bool(row["baseline_false_positive"])
        or not _read_bool(row["mutation_applied"])
        or not _read_bool(row["restoration_verified"])
        for row in rows
    ):
        raise ValueError("a tamper trial failed a technical validity gate")
    with (output_dir / "scenario_summary.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        summaries = list(csv.DictReader(handle))
    if len(summaries) != len(SCENARIOS):
        raise ValueError("scenario summary cardinality is incorrect")
    for line in (output_dir / "SHA256SUMS").read_text(
        encoding="utf-8"
    ).splitlines():
        expected_hash, relative = line.split("  ", 1)
        if _sha256(output_dir / relative) != expected_hash:
            raise ValueError(f"hash mismatch: {relative}")
    return {
        "status": "PASS",
        "scenario_count": len(SCENARIOS),
        "trials_per_scenario": expected_per_scenario,
        "trial_count": expected_total,
        "expected_outcomes_met": sum(
            _read_bool(row["expected_outcome_met"]) for row in rows
        ),
        "technical_validity_failures": 0,
    }


async def run_experiment(config: TamperConfig) -> Path:
    output_dir = config.output_root / config.batch_id
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = _manifest(config, output_dir)
    trials: list[TamperTrial] = []
    _persist(output_dir, manifest, trials)

    timeout = httpx.Timeout(config.timeout_seconds)
    traditional_admin = await AsyncConnection.connect(
        _database_conninfo("TRADITIONAL", application=False),
        row_factory=dict_row,
    )
    fabric_admin = await AsyncConnection.connect(
        _database_conninfo("FABRIC", application=False),
        row_factory=dict_row,
    )
    try:
        async with (
            httpx.AsyncClient(
                base_url=config.traditional_url, timeout=timeout
            ) as traditional_client,
            httpx.AsyncClient(
                base_url=config.fabric_url, timeout=timeout
            ) as fabric_client,
            httpx.AsyncClient(
                base_url=config.fabric_gateway_url, timeout=timeout
            ) as gateway_client,
        ):
            for client in (traditional_client, fabric_client):
                health = await client.get("/healthz")
                health.raise_for_status()
            gateway_health = await gateway_client.get("/healthz")
            gateway_health.raise_for_status()

            experiment = TamperExperiment(
                config,
                traditional_client,
                fabric_client,
                gateway_client,
                traditional_admin,
                fabric_admin,
            )
            for definition in SCENARIOS:
                for trial in range(1, config.trials + 1):
                    print(
                        f"begin {definition.scenario_id} "
                        f"trial {trial}/{config.trials}",
                        flush=True,
                    )
                    result = await experiment.run_trial(definition, trial)
                    trials.append(result)
                    _persist(output_dir, manifest, trials)
                    print(
                        f"complete {result.trial_id}: {result.outcome}",
                        flush=True,
                    )
    except BaseException as exc:
        manifest["status"] = "failed"
        manifest["ended_at_utc"] = _utc_now().isoformat()
        manifest["failure_type"] = type(exc).__name__
        manifest["failure_message"] = " ".join(str(exc).split())[:500]
        _persist(output_dir, manifest, trials)
        raise
    finally:
        await traditional_admin.close()
        await fabric_admin.close()

    manifest["status"] = "completed"
    manifest["ended_at_utc"] = _utc_now().isoformat()
    manifest["observed_trial_count"] = len(trials)
    manifest["expected_outcomes_met"] = sum(
        item.expected_outcome_met for item in trials
    )
    _persist(output_dir, manifest, trials)
    _write_hashes(output_dir)
    verification = verify_output(output_dir)
    _write_json(output_dir / "verification.json", verification)
    _write_hashes(output_dir)
    verification = verify_output(output_dir)
    _freeze(output_dir)
    print(json.dumps(verification, indent=2, sort_keys=True))
    return output_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the separate RQ2 tamper-evidence experiment"
    )
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--rq1-batch-id", required=True)
    parser.add_argument(
        "--traditional-url", default="http://127.0.0.1:18000"
    )
    parser.add_argument(
        "--fabric-url", default="http://127.0.0.1:18001"
    )
    parser.add_argument(
        "--fabric-gateway-url", default="http://127.0.0.1:18081"
    )
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=4202)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--output-root", type=Path, default=Path("results/tamper")
    )
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    output_dir = arguments.output_root / arguments.batch_id
    if arguments.verify_only:
        print(
            json.dumps(
                verify_output(output_dir), indent=2, sort_keys=True
            )
        )
        return
    config = TamperConfig(
        batch_id=arguments.batch_id,
        rq1_batch_id=arguments.rq1_batch_id,
        traditional_url=arguments.traditional_url,
        fabric_url=arguments.fabric_url,
        fabric_gateway_url=arguments.fabric_gateway_url,
        trials=arguments.trials,
        seed=arguments.seed,
        timeout_seconds=arguments.timeout_seconds,
        output_root=arguments.output_root,
        pilot=arguments.pilot,
    )
    output = asyncio.run(run_experiment(config))
    print(f"OUTPUT_DIR={output}")


if __name__ == "__main__":
    main()
