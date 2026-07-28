from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRADITIONAL_ROOT = PROJECT_ROOT / "architecture-traditional"
sys.path.insert(0, str(TRADITIONAL_ROOT))

import app.adapter as adapter_module  # noqa: E402
from app.adapter import TraditionalPostgresAdapter  # noqa: E402
from shared.contracts.errors import (  # noqa: E402
    AccessDeniedError,
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from shared.hashing.canonical import payload_sha256  # noqa: E402
from shared.schemas import (  # noqa: E402
    AccessDecision,
    ActorContext,
    AuthorizationRule,
    CreateRecordRequest,
    EvaluateAccessRequest,
    FHIRResourceEnvelope,
    OperationId,
    RecordLocator,
    ResourceType,
    RetrieveAuditRequest,
    RetrieveRecordRequest,
    UpdateAuthorizationRequest,
    VerifyIntegrityRequest,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason=(
        "set RUN_POSTGRES_INTEGRATION=1 and export the PostgreSQL "
        "environment variables to run integration tests"
    ),
)


def _conninfo() -> str:
    required_names = (
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "APP_DB_USER",
        "APP_DB_PASSWORD",
    )
    missing_names = [
        name
        for name in required_names
        if not os.environ.get(name)
    ]
    if missing_names:
        pytest.fail(
            "missing PostgreSQL environment variables: "
            + ", ".join(missing_names)
        )

    return make_conninfo(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=os.environ["POSTGRES_PORT"],
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["APP_DB_USER"],
        password=os.environ["APP_DB_PASSWORD"],
    )


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:24]}"


def _actor(prefix: str) -> ActorContext:
    token = uuid4().hex[:16]
    return ActorContext(
        actor_id=f"{prefix}-{token}",
        organization_id=f"org-{token}",
    )


def _record(record_id: str) -> RecordLocator:
    return RecordLocator(
        resource_type=ResourceType.PATIENT,
        resource_id=record_id,
    )


def _resource(record_id: str) -> FHIRResourceEnvelope:
    payload = {
        "resourceType": "Patient",
        "id": record_id,
        "active": True,
        "name": [
            {
                "family": "Integration",
                "given": ["Traditional"],
            }
        ],
    }
    return FHIRResourceEnvelope.from_payload(payload)


def test_application_role_is_least_privileged() -> None:
    async def scenario() -> None:
        async with await AsyncConnection.connect(
            _conninfo(),
            row_factory=dict_row,
        ) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    current_user AS role_name,
                    rolsuper,
                    rolcreatedb,
                    rolcreaterole,
                    rolinherit,
                    rolreplication,
                    rolbypassrls,
                    has_schema_privilege(
                        current_user,
                        'public',
                        'CREATE'
                    ) AS can_create_in_schema,
                    has_table_privilege(
                        current_user,
                        'public.clinical_records',
                        'INSERT'
                    ) AS can_insert_clinical,
                    has_table_privilege(
                        current_user,
                        'public.clinical_records',
                        'UPDATE'
                    ) AS can_update_clinical,
                    has_table_privilege(
                        current_user,
                        'public.authorization_rules',
                        'UPDATE'
                    ) AS can_update_authorization,
                    has_table_privilege(
                        current_user,
                        'public.audit_events',
                        'INSERT'
                    ) AS can_insert_audit,
                    has_table_privilege(
                        current_user,
                        'public.audit_events',
                        'UPDATE'
                    ) AS can_update_audit,
                    has_table_privilege(
                        current_user,
                        'public.audit_events',
                        'DELETE'
                    ) AS can_delete_audit
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
            role = await cursor.fetchone()

        assert role is not None
        assert role["role_name"] == os.environ["APP_DB_USER"]
        assert role["rolsuper"] is False
        assert role["rolcreatedb"] is False
        assert role["rolcreaterole"] is False
        assert role["rolinherit"] is False
        assert role["rolreplication"] is False
        assert role["rolbypassrls"] is False
        assert role["can_create_in_schema"] is False
        assert role["can_insert_clinical"] is True
        assert role["can_update_clinical"] is False
        assert role["can_update_authorization"] is True
        assert role["can_insert_audit"] is True
        assert role["can_update_audit"] is False
        assert role["can_delete_audit"] is False

    asyncio.run(scenario())


def test_failed_audit_insert_rolls_back_record_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        record_id = _unique_id("it-rollback")
        resource = _resource(record_id)
        actor = _actor("administrator")
        request = CreateRecordRequest(
            resource=resource,
            actor=actor,
        )

        async with TraditionalPostgresAdapter(
            _conninfo(),
            max_pool_size=2,
        ) as adapter:
            with monkeypatch.context() as audit_patch:
                audit_patch.setattr(
                    adapter_module,
                    "audit_event_sha256",
                    lambda *_args: "invalid",
                )
                with pytest.raises(CheckViolation):
                    await adapter.create_record(request)

            created = await adapter.create_record(request)
            assert created.record == _record(record_id)

            audit = await adapter.retrieve_audit(
                RetrieveAuditRequest(record=_record(record_id))
            )
            assert len(audit.events) == 1
            assert audit.events[0].action is OperationId.CREATE_RECORD

    asyncio.run(scenario())


def test_op1_through_op6_end_to_end() -> None:
    async def scenario() -> None:
        record_id = _unique_id("it-e2e")
        record = _record(record_id)
        resource = _resource(record_id)
        administrator = _actor("administrator")
        clinician = _actor("clinician")

        async with TraditionalPostgresAdapter(
            _conninfo(),
            max_pool_size=10,
        ) as adapter:
            created = await adapter.create_record(
                CreateRecordRequest(
                    resource=resource,
                    actor=administrator,
                )
            )
            assert created.record == record
            assert (
                created.integrity.payload_hash
                == payload_sha256(resource.payload)
            )

            default_decision = await adapter.evaluate_access(
                EvaluateAccessRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert default_decision.decision is AccessDecision.DENY
            assert default_decision.matched_rule_id is None

            with pytest.raises(AccessDeniedError):
                await adapter.retrieve_record(
                    RetrieveRecordRequest(
                        record=record,
                        actor=clinician,
                    )
                )

            rule = AuthorizationRule(
                rule_id=_unique_id("rule-allow"),
                record=record,
                principal=clinician,
                decision=AccessDecision.ALLOW,
            )
            updated = await adapter.update_authorization(
                UpdateAuthorizationRequest(
                    rule=rule,
                    actor=administrator,
                )
            )
            assert updated.rule == rule

            allowed = await adapter.evaluate_access(
                EvaluateAccessRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert allowed.decision is AccessDecision.ALLOW
            assert allowed.matched_rule_id == rule.rule_id

            retrieved = await adapter.retrieve_record(
                RetrieveRecordRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert retrieved.resource == resource

            integrity = await adapter.verify_integrity(
                VerifyIntegrityRequest(record=record)
            )
            assert integrity.record == record
            assert integrity.matches is True
            assert (
                integrity.observed_hash
                == created.integrity.payload_hash
            )
            assert (
                integrity.authoritative_evidence
                == created.integrity
            )

            audit = await adapter.retrieve_audit(
                RetrieveAuditRequest(record=record)
            )
            assert [event.action for event in audit.events] == [
                OperationId.CREATE_RECORD,
                OperationId.EVALUATE_ACCESS,
                OperationId.UPDATE_AUTHORIZATION,
                OperationId.EVALUATE_ACCESS,
            ]
            assert [event.decision for event in audit.events] == [
                None,
                AccessDecision.DENY,
                None,
                AccessDecision.ALLOW,
            ]
            assert [event.sequence for event in audit.events] == sorted(
                event.sequence
                for event in audit.events
            )
            assert await adapter.first_invalid_audit_sequence() is None

    asyncio.run(scenario())


def test_strict_creation_and_missing_record_errors() -> None:
    async def scenario() -> None:
        actor = _actor("administrator")
        principal = _actor("clinician")
        record_id = _unique_id("it-errors")
        record = _record(record_id)
        resource = _resource(record_id)

        missing_record = _record(_unique_id("it-missing"))
        missing_rule = AuthorizationRule(
            rule_id=_unique_id("rule-missing"),
            record=missing_record,
            principal=principal,
            decision=AccessDecision.ALLOW,
        )

        async with TraditionalPostgresAdapter(
            _conninfo(),
            max_pool_size=10,
        ) as adapter:
            create_request = CreateRecordRequest(
                resource=resource,
                actor=actor,
            )
            await adapter.create_record(create_request)

            with pytest.raises(RecordAlreadyExistsError):
                await adapter.create_record(create_request)

            with pytest.raises(RecordNotFoundError):
                await adapter.retrieve_record(
                    RetrieveRecordRequest(
                        record=missing_record,
                        actor=principal,
                    )
                )

            with pytest.raises(RecordNotFoundError):
                await adapter.update_authorization(
                    UpdateAuthorizationRequest(
                        rule=missing_rule,
                        actor=actor,
                    )
                )

            with pytest.raises(RecordNotFoundError):
                await adapter.evaluate_access(
                    EvaluateAccessRequest(
                        record=missing_record,
                        actor=principal,
                    )
                )

            with pytest.raises(RecordNotFoundError):
                await adapter.verify_integrity(
                    VerifyIntegrityRequest(
                        record=missing_record,
                    )
                )

            with pytest.raises(RecordNotFoundError):
                await adapter.retrieve_audit(
                    RetrieveAuditRequest(
                        record=missing_record,
                    )
                )

            assert await adapter.first_invalid_audit_sequence() is None

    asyncio.run(scenario())


def test_authorization_upsert_replaces_rule_and_decision() -> None:
    async def scenario() -> None:
        record_id = _unique_id("it-upsert")
        record = _record(record_id)
        resource = _resource(record_id)
        administrator = _actor("administrator")
        clinician = _actor("clinician")

        allow_rule = AuthorizationRule(
            rule_id=_unique_id("rule-allow"),
            record=record,
            principal=clinician,
            decision=AccessDecision.ALLOW,
        )
        deny_rule = AuthorizationRule(
            rule_id=_unique_id("rule-deny"),
            record=record,
            principal=clinician,
            decision=AccessDecision.DENY,
        )

        async with TraditionalPostgresAdapter(
            _conninfo(),
            max_pool_size=10,
        ) as adapter:
            await adapter.create_record(
                CreateRecordRequest(
                    resource=resource,
                    actor=administrator,
                )
            )
            await adapter.update_authorization(
                UpdateAuthorizationRequest(
                    rule=allow_rule,
                    actor=administrator,
                )
            )

            allowed = await adapter.evaluate_access(
                EvaluateAccessRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert allowed.decision is AccessDecision.ALLOW
            assert allowed.matched_rule_id == allow_rule.rule_id

            await adapter.retrieve_record(
                RetrieveRecordRequest(
                    record=record,
                    actor=clinician,
                )
            )

            await adapter.update_authorization(
                UpdateAuthorizationRequest(
                    rule=deny_rule,
                    actor=administrator,
                )
            )

            denied = await adapter.evaluate_access(
                EvaluateAccessRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert denied.decision is AccessDecision.DENY
            assert denied.matched_rule_id == deny_rule.rule_id

            with pytest.raises(AccessDeniedError):
                await adapter.retrieve_record(
                    RetrieveRecordRequest(
                        record=record,
                        actor=clinician,
                    )
                )

            audit = await adapter.retrieve_audit(
                RetrieveAuditRequest(record=record)
            )
            assert [event.action for event in audit.events] == [
                OperationId.CREATE_RECORD,
                OperationId.UPDATE_AUTHORIZATION,
                OperationId.EVALUATE_ACCESS,
                OperationId.UPDATE_AUTHORIZATION,
                OperationId.EVALUATE_ACCESS,
            ]
            assert [event.decision for event in audit.events] == [
                None,
                None,
                AccessDecision.ALLOW,
                None,
                AccessDecision.DENY,
            ]
            assert await adapter.first_invalid_audit_sequence() is None

    asyncio.run(scenario())


def test_ten_concurrent_clients_preserve_global_audit_chain() -> None:
    async def scenario() -> None:
        actor = _actor("concurrent-client")
        resources = [
            _resource(_unique_id(f"it-c{index:02d}"))
            for index in range(10)
        ]
        start = asyncio.Event()

        async with TraditionalPostgresAdapter(
            _conninfo(),
            max_pool_size=10,
        ) as adapter:
            async def create_after_start(
                resource: FHIRResourceEnvelope,
            ):
                await start.wait()
                return await adapter.create_record(
                    CreateRecordRequest(
                        resource=resource,
                        actor=actor,
                    )
                )

            tasks = [
                asyncio.create_task(
                    create_after_start(resource)
                )
                for resource in resources
            ]
            await asyncio.sleep(0)
            start.set()
            results = await asyncio.gather(*tasks)

            assert {
                result.record.resource_id
                for result in results
            } == {
                resource.resource_id
                for resource in resources
            }

            audits = await asyncio.gather(
                *(
                    adapter.retrieve_audit(
                        RetrieveAuditRequest(
                            record=_record(resource.resource_id),
                        )
                    )
                    for resource in resources
                )
            )
            assert all(
                len(audit.events) == 1
                and audit.events[0].action
                is OperationId.CREATE_RECORD
                for audit in audits
            )
            assert await adapter.first_invalid_audit_sequence() is None

    asyncio.run(scenario())
