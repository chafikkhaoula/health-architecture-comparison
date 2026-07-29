from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from shared.contracts.errors import (
    AccessDeniedError,
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from shared.hashing.canonical import payload_sha256
from shared.schemas import (
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FABRIC_APP_ROOT = PROJECT_ROOT / "architecture-fabric" / "app"
SPEC = importlib.util.spec_from_file_location(
    "health_architecture_fabric_integration_app",
    FABRIC_APP_ROOT / "__init__.py",
    submodule_search_locations=[str(FABRIC_APP_ROOT)],
)
assert SPEC is not None and SPEC.loader is not None
FABRIC_APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FABRIC_APP
SPEC.loader.exec_module(FABRIC_APP)
FabricAdapter = FABRIC_APP.FabricAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FABRIC_INTEGRATION") != "1",
    reason=(
        "set RUN_FABRIC_INTEGRATION=1 and export the Fabric "
        "PostgreSQL and Gateway environment variables"
    ),
)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"missing environment variable: {name}")
    return value


def _app_conninfo() -> str:
    return make_conninfo(
        host=os.environ.get("FABRIC_POSTGRES_HOST", "127.0.0.1"),
        port=_required("FABRIC_POSTGRES_PORT"),
        dbname=_required("POSTGRES_DB"),
        user=_required("APP_DB_USER"),
        password=_required("APP_DB_PASSWORD"),
    )


def _admin_conninfo() -> str:
    return make_conninfo(
        host=os.environ.get("FABRIC_POSTGRES_HOST", "127.0.0.1"),
        port=_required("FABRIC_POSTGRES_PORT"),
        dbname=_required("POSTGRES_DB"),
        user=_required("POSTGRES_ADMIN_USER"),
        password=_required("POSTGRES_ADMIN_PASSWORD"),
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
    return FHIRResourceEnvelope.from_payload(
        {
            "resourceType": "Patient",
            "id": record_id,
            "active": True,
            "name": [
                {
                    "family": "Integration",
                    "given": ["Fabric"],
                }
            ],
        }
    )


def _adapter() -> object:
    return FabricAdapter(
        postgres_conninfo=_app_conninfo(),
        gateway_url=_required("FABRIC_GATEWAY_URL"),
        max_pool_size=10,
    )


def test_fabric_payload_role_is_least_privileged() -> None:
    async def scenario() -> None:
        async with await AsyncConnection.connect(
            _app_conninfo(),
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
                        'public.clinical_payloads',
                        'INSERT'
                    ) AS can_insert_payload,
                    has_table_privilege(
                        current_user,
                        'public.clinical_payloads',
                        'UPDATE'
                    ) AS can_update_payload,
                    has_table_privilege(
                        current_user,
                        'public.clinical_payloads',
                        'DELETE'
                    ) AS can_delete_payload
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
        assert role["can_insert_payload"] is True
        assert role["can_update_payload"] is False
        assert role["can_delete_payload"] is False

    asyncio.run(scenario())


def test_fabric_op1_through_op6_end_to_end() -> None:
    async def scenario() -> None:
        record_id = _unique_id("fabric-e2e")
        resource = _resource(record_id)
        record = _record(record_id)
        administrator = _actor("administrator")
        clinician = _actor("clinician")

        async with _adapter() as adapter:
            created = await adapter.create_record(
                CreateRecordRequest(
                    resource=resource,
                    actor=administrator,
                )
            )
            assert created.integrity.payload_hash == payload_sha256(
                resource.payload
            )
            receipt = adapter.last_transaction_receipt
            assert receipt is not None
            assert receipt.commit_status == "VALID"
            assert receipt.validation_code == 0
            assert receipt.transaction_id is not None

            denied = await adapter.evaluate_access(
                EvaluateAccessRequest(
                    record=record,
                    actor=clinician,
                )
            )
            assert denied.decision is AccessDecision.DENY
            assert denied.matched_rule_id is None
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
            await adapter.update_authorization(
                UpdateAuthorizationRequest(
                    rule=rule,
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
            assert integrity.matches is True

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

    asyncio.run(scenario())


def test_fabric_strict_creation_and_missing_record_errors() -> None:
    async def scenario() -> None:
        record_id = _unique_id("fabric-errors")
        resource = _resource(record_id)
        actor = _actor("administrator")
        principal = _actor("clinician")
        missing = _record(_unique_id("fabric-missing"))
        missing_rule = AuthorizationRule(
            rule_id=_unique_id("rule-missing"),
            record=missing,
            principal=principal,
            decision=AccessDecision.ALLOW,
        )

        async with _adapter() as adapter:
            request = CreateRecordRequest(
                resource=resource,
                actor=actor,
            )
            await adapter.create_record(request)
            with pytest.raises(RecordAlreadyExistsError):
                await adapter.create_record(request)
            with pytest.raises(RecordNotFoundError):
                await adapter.retrieve_record(
                    RetrieveRecordRequest(
                        record=missing,
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
                        record=missing,
                        actor=principal,
                    )
                )
            with pytest.raises(RecordNotFoundError):
                await adapter.verify_integrity(
                    VerifyIntegrityRequest(record=missing)
                )
            with pytest.raises(RecordNotFoundError):
                await adapter.retrieve_audit(
                    RetrieveAuditRequest(record=missing)
                )

    asyncio.run(scenario())


def test_fabric_detects_privileged_off_chain_rewrite() -> None:
    async def scenario() -> None:
        record_id = _unique_id("fabric-tamper")
        resource = _resource(record_id)
        record = _record(record_id)

        async with _adapter() as adapter:
            await adapter.create_record(
                CreateRecordRequest(
                    resource=resource,
                    actor=_actor("administrator"),
                )
            )
            baseline = await adapter.verify_integrity(
                VerifyIntegrityRequest(record=record)
            )
            assert baseline.matches is True

            async with await AsyncConnection.connect(
                _admin_conninfo()
            ) as connection:
                await connection.execute(
                    """
                    UPDATE clinical_payloads
                    SET payload = jsonb_set(
                        payload,
                        '{active}',
                        'false'::jsonb
                    )
                    WHERE resource_type = %s
                      AND resource_id = %s
                    """,
                    ("Patient", record_id),
                )
                await connection.commit()

            tampered = await adapter.verify_integrity(
                VerifyIntegrityRequest(record=record)
            )
            assert tampered.matches is False
            assert (
                tampered.authoritative_evidence
                == baseline.authoritative_evidence
            )

    asyncio.run(scenario())


def test_fabric_ten_concurrent_records_commit_validly() -> None:
    async def scenario() -> None:
        administrator = _actor("concurrent-administrator")
        resources = [
            _resource(_unique_id(f"fabric-c{index:02d}"))
            for index in range(10)
        ]
        start = asyncio.Event()

        async with _adapter() as adapter:

            async def create_after_start(
                resource: FHIRResourceEnvelope,
            ):
                await start.wait()
                result = await adapter.create_record(
                    CreateRecordRequest(
                        resource=resource,
                        actor=administrator,
                    )
                )
                return result, adapter.last_transaction_receipt

            tasks = [
                asyncio.create_task(create_after_start(resource))
                for resource in resources
            ]
            await asyncio.sleep(0)
            start.set()
            outcomes = await asyncio.gather(*tasks)

            assert {outcome[0].record.resource_id for outcome in outcomes} == {
                resource.resource_id for resource in resources
            }
            receipts = [outcome[1] for outcome in outcomes]
            assert all(
                receipt is not None
                and receipt.commit_status == "VALID"
                and receipt.validation_code == 0
                for receipt in receipts
            )
            assert (
                len(
                    {
                        receipt.transaction_id
                        for receipt in receipts
                        if receipt is not None
                    }
                )
                == 10
            )

            audits = await asyncio.gather(
                *(
                    adapter.retrieve_audit(
                        RetrieveAuditRequest(
                            record=_record(resource.resource_id)
                        )
                    )
                    for resource in resources
                )
            )
            assert all(
                len(audit.events) == 1
                and audit.events[0].action is OperationId.CREATE_RECORD
                and audit.events[0].sequence == 1
                for audit in audits
            )

    asyncio.run(scenario())
