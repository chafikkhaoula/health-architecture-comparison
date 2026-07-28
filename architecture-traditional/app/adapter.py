from __future__ import annotations

from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from shared.contracts.errors import (
    AccessDeniedError,
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from shared.hashing.audit import (
    StoredAuditLink,
    audit_event_sha256,
    first_invalid_audit_sequence,
)
from shared.hashing.canonical import payload_sha256
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuditEvent,
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    FHIRResourceEnvelope,
    IntegrityEvidence,
    OperationId,
    RecordLocator,
    ResourceType,
    RetrieveAuditRequest,
    RetrieveAuditResult,
    RetrieveRecordRequest,
    RetrieveRecordResult,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)

DatabaseConnection = AsyncConnection[dict[str, Any]]

# One database-wide chain is shared by every clinical record.
_AUDIT_LOCK_KEY = int.from_bytes(
    b"HACaudit",
    byteorder="big",
    signed=True,
)


class TraditionalPostgresAdapter:
    """PostgreSQL implementation of the frozen OP1-OP6 contract."""

    def __init__(
        self,
        conninfo: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        pool_timeout_seconds: float = 30.0,
    ) -> None:
        if min_pool_size < 1:
            raise ValueError("min_pool_size must be at least 1")
        if max_pool_size < min_pool_size:
            raise ValueError(
                "max_pool_size must be greater than or equal to "
                "min_pool_size"
            )

        self._pool_timeout_seconds = pool_timeout_seconds
        self._pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=min_pool_size,
            max_size=max_pool_size,
            timeout=pool_timeout_seconds,
            open=False,
            kwargs={
                "autocommit": False,
                "row_factory": dict_row,
            },
            name="traditional-postgres",
        )

    async def open(self) -> None:
        await self._pool.open(
            wait=True,
            timeout=self._pool_timeout_seconds,
        )

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        resource = request.resource
        record = RecordLocator(
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )
        digest = payload_sha256(resource.payload)
        integrity = IntegrityEvidence(payload_hash=digest)

        try:
            async with self._pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO clinical_records (
                            resource_type,
                            resource_id,
                            payload,
                            payload_hash
                        )
                        VALUES (%s, %s, %s, %s)
                        """,
                        (
                            record.resource_type.value,
                            record.resource_id,
                            Jsonb(resource.payload),
                            digest,
                        ),
                    )

                    await self._append_audit_event(
                        connection,
                        record=record,
                        actor=request.actor,
                        action=OperationId.CREATE_RECORD,
                    )
        except UniqueViolation as exc:
            if exc.diag.constraint_name == "clinical_records_pk":
                raise RecordAlreadyExistsError(
                    f"record already exists: "
                    f"{record.resource_type.value}/{record.resource_id}"
                ) from exc
            raise

        return CreateRecordResult(
            record=record,
            integrity=integrity,
        )

    async def retrieve_record(
        self,
        request: RetrieveRecordRequest,
    ) -> RetrieveRecordResult:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    records.payload,
                    rules.decision
                FROM clinical_records AS records
                LEFT JOIN authorization_rules AS rules
                    ON rules.resource_type = records.resource_type
                    AND rules.resource_id = records.resource_id
                    AND rules.principal_actor_id = %s
                    AND rules.principal_organization_id = %s
                WHERE records.resource_type = %s
                  AND records.resource_id = %s
                """,
                (
                    request.actor.actor_id,
                    request.actor.organization_id,
                    request.record.resource_type.value,
                    request.record.resource_id,
                ),
            )
            row = await cursor.fetchone()

        if row is None:
            self._raise_record_not_found(request.record)

        if row["decision"] != AccessDecision.ALLOW.value:
            raise AccessDeniedError(
                f"access denied: "
                f"{request.record.resource_type.value}/"
                f"{request.record.resource_id}"
            )

        resource = FHIRResourceEnvelope(
            resource_type=request.record.resource_type,
            resource_id=request.record.resource_id,
            payload=row["payload"],
        )
        return RetrieveRecordResult(resource=resource)

    async def update_authorization(
        self,
        request: UpdateAuthorizationRequest,
    ) -> UpdateAuthorizationResult:
        rule = request.rule

        async with self._pool.connection() as connection:
            async with connection.transaction():
                await self._require_record(
                    connection,
                    rule.record,
                )

                await connection.execute(
                    """
                    INSERT INTO authorization_rules (
                        rule_id,
                        resource_type,
                        resource_id,
                        principal_actor_id,
                        principal_organization_id,
                        decision
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT ON CONSTRAINT
                        authorization_rules_principal_unique
                    DO UPDATE SET
                        rule_id = EXCLUDED.rule_id,
                        decision = EXCLUDED.decision,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        rule.rule_id,
                        rule.record.resource_type.value,
                        rule.record.resource_id,
                        rule.principal.actor_id,
                        rule.principal.organization_id,
                        rule.decision.value,
                    ),
                )

                await self._append_audit_event(
                    connection,
                    record=rule.record,
                    actor=request.actor,
                    action=OperationId.UPDATE_AUTHORIZATION,
                )

        return UpdateAuthorizationResult(rule=rule)

    async def evaluate_access(
        self,
        request: EvaluateAccessRequest,
    ) -> EvaluateAccessResult:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT
                        rules.rule_id,
                        rules.decision
                    FROM clinical_records AS records
                    LEFT JOIN authorization_rules AS rules
                        ON rules.resource_type = records.resource_type
                        AND rules.resource_id = records.resource_id
                        AND rules.principal_actor_id = %s
                        AND rules.principal_organization_id = %s
                    WHERE records.resource_type = %s
                      AND records.resource_id = %s
                    """,
                    (
                        request.actor.actor_id,
                        request.actor.organization_id,
                        request.record.resource_type.value,
                        request.record.resource_id,
                    ),
                )
                row = await cursor.fetchone()

                if row is None:
                    self._raise_record_not_found(request.record)

                matched_rule_id = row["rule_id"]
                decision = (
                    AccessDecision(row["decision"])
                    if row["decision"] is not None
                    else AccessDecision.DENY
                )

                result = EvaluateAccessResult(
                    record=request.record,
                    actor=request.actor,
                    decision=decision,
                    matched_rule_id=matched_rule_id,
                )

                await self._append_audit_event(
                    connection,
                    record=request.record,
                    actor=request.actor,
                    action=OperationId.EVALUATE_ACCESS,
                    decision=decision,
                )

        return result

    async def verify_integrity(
        self,
        request: VerifyIntegrityRequest,
    ) -> VerifyIntegrityResult:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT payload, payload_hash
                FROM clinical_records
                WHERE resource_type = %s
                  AND resource_id = %s
                """,
                (
                    request.record.resource_type.value,
                    request.record.resource_id,
                ),
            )
            row = await cursor.fetchone()

        if row is None:
            self._raise_record_not_found(request.record)

        observed_hash = payload_sha256(row["payload"])
        authoritative_evidence = IntegrityEvidence(
            payload_hash=row["payload_hash"],
        )

        return VerifyIntegrityResult(
            record=request.record,
            authoritative_evidence=authoritative_evidence,
            observed_hash=observed_hash,
            matches=(
                observed_hash
                == authoritative_evidence.payload_hash
            ),
        )

    async def retrieve_audit(
        self,
        request: RetrieveAuditRequest,
    ) -> RetrieveAuditResult:
        async with self._pool.connection() as connection:
            await self._require_record(
                connection,
                request.record,
            )
            cursor = await connection.execute(
                """
                SELECT
                    sequence,
                    event_id,
                    resource_type,
                    resource_id,
                    actor_id,
                    organization_id,
                    action,
                    decision,
                    event_timestamp
                FROM audit_events
                WHERE resource_type = %s
                  AND resource_id = %s
                ORDER BY sequence
                """,
                (
                    request.record.resource_type.value,
                    request.record.resource_id,
                ),
            )
            rows = await cursor.fetchall()

        events = tuple(
            self._audit_event_from_row(row)
            for row in rows
        )
        return RetrieveAuditResult(
            record=request.record,
            events=events,
        )

    async def first_invalid_audit_sequence(self) -> int | None:
        """Verify the complete database-wide audit chain."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    sequence,
                    event_id,
                    resource_type,
                    resource_id,
                    actor_id,
                    organization_id,
                    action,
                    decision,
                    event_timestamp,
                    previous_hash,
                    event_hash
                FROM audit_events
                ORDER BY sequence
                """
            )
            rows = await cursor.fetchall()

        links = tuple(
            StoredAuditLink(
                event=self._audit_event_from_row(row),
                previous_hash=row["previous_hash"],
                event_hash=row["event_hash"],
            )
            for row in rows
        )
        return first_invalid_audit_sequence(links)

    async def _require_record(
        self,
        connection: DatabaseConnection,
        record: RecordLocator,
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT 1
            FROM clinical_records
            WHERE resource_type = %s
              AND resource_id = %s
            """,
            (
                record.resource_type.value,
                record.resource_id,
            ),
        )
        if await cursor.fetchone() is None:
            self._raise_record_not_found(record)

    async def _append_audit_event(
        self,
        connection: DatabaseConnection,
        *,
        record: RecordLocator,
        actor: ActorContext,
        action: OperationId,
        decision: AccessDecision | None = None,
    ) -> StoredAuditLink:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_AUDIT_LOCK_KEY,),
        )

        cursor = await connection.execute(
            """
            SELECT sequence, event_hash
            FROM audit_events
            ORDER BY sequence DESC
            LIMIT 1
            """
        )
        latest = await cursor.fetchone()

        if latest is None:
            sequence = 1
            previous_hash = None
        else:
            sequence = latest["sequence"] + 1
            previous_hash = latest["event_hash"]

        event = AuditEvent(
            event_id=f"event-{uuid4().hex}",
            sequence=sequence,
            record=record,
            actor=actor,
            action=action,
            decision=decision,
            timestamp=datetime.now(timezone.utc),
        )
        event_hash = audit_event_sha256(
            event,
            previous_hash,
        )

        await connection.execute(
            """
            INSERT INTO audit_events (
                sequence,
                event_id,
                resource_type,
                resource_id,
                actor_id,
                organization_id,
                action,
                decision,
                event_timestamp,
                previous_hash,
                event_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.sequence,
                event.event_id,
                event.record.resource_type.value,
                event.record.resource_id,
                event.actor.actor_id,
                event.actor.organization_id,
                event.action.value,
                (
                    event.decision.value
                    if event.decision is not None
                    else None
                ),
                event.timestamp,
                previous_hash,
                event_hash,
            ),
        )

        return StoredAuditLink(
            event=event,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )

    @staticmethod
    def _audit_event_from_row(
        row: dict[str, Any],
    ) -> AuditEvent:
        return AuditEvent(
            event_id=row["event_id"],
            sequence=row["sequence"],
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
        )

    @staticmethod
    def _raise_record_not_found(
        record: RecordLocator,
    ) -> None:
        raise RecordNotFoundError(
            f"record not found: "
            f"{record.resource_type.value}/{record.resource_id}"
        )
