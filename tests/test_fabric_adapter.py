from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from shared.contracts import ArchitectureAdapter
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
    "health_architecture_fabric_app",
    FABRIC_APP_ROOT / "__init__.py",
    submodule_search_locations=[str(FABRIC_APP_ROOT)],
)
assert SPEC is not None and SPEC.loader is not None
FABRIC_APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FABRIC_APP
SPEC.loader.exec_module(FABRIC_APP)

FabricAdapter = FABRIC_APP.FabricAdapter
GatewayCallError = FABRIC_APP.GatewayCallError
GatewayReceipt = FABRIC_APP.GatewayReceipt


def _actor(actor_id: str, organization_id: str) -> ActorContext:
    return ActorContext(
        actor_id=actor_id,
        organization_id=organization_id,
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
        }
    )


class MemoryPayloadStore:
    def __init__(self) -> None:
        self.payloads: dict[tuple[str, str], dict[str, Any]] = {}
        self.opened = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    @asynccontextmanager
    async def stage_create(
        self,
        resource: FHIRResourceEnvelope,
    ) -> AsyncIterator[None]:
        key = (
            resource.resource_type.value,
            resource.resource_id,
        )
        if key in self.payloads:
            raise RecordAlreadyExistsError("duplicate")
        yield
        self.payloads[key] = resource.payload

    async def get_payload(
        self,
        record: RecordLocator,
    ) -> dict[str, Any]:
        key = (record.resource_type.value, record.resource_id)
        try:
            return self.payloads[key]
        except KeyError as exc:
            raise RecordNotFoundError("missing") from exc


class MemoryGateway:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.rules: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.audit: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.opened = False
        self.fail_create = False
        self.transaction_index = 0

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def submit(
        self,
        function: str,
        arguments: tuple[str, ...],
        *,
        endorsing_organizations: tuple[str, ...] = (),
    ) -> Any:
        self.transaction_index += 1
        transaction_id = f"{self.transaction_index:064x}"

        if function == "CreateRecord":
            if self.fail_create:
                raise GatewayCallError(
                    "simulated failure",
                    code="GATEWAY_ERROR",
                    stage="endorse",
                    transaction_id=transaction_id,
                )
            (
                resource_type,
                resource_id,
                payload_hash,
                actor_id,
                organization_id,
            ) = arguments
            key = (resource_type, resource_id)
            if key in self.records:
                raise GatewayCallError(
                    "duplicate",
                    code="RECORD_ALREADY_EXISTS",
                    stage="endorse",
                    transaction_id=transaction_id,
                )
            evidence = {
                "resourceType": resource_type,
                "resourceId": resource_id,
                "algorithm": "sha256",
                "canonicalization": "RFC8785",
                "payloadHash": payload_hash,
                "createdAt": "2026-01-01T00:00:00Z",
            }
            self.records[key] = evidence
            self._append_audit(
                key,
                actor_id,
                organization_id,
                "OP1",
                None,
                transaction_id,
            )
            return self._receipt(evidence, transaction_id)

        if function == "UpdateAuthorization":
            (
                rule_id,
                resource_type,
                resource_id,
                principal_actor_id,
                principal_organization_id,
                decision,
                actor_id,
                organization_id,
            ) = arguments
            key = (resource_type, resource_id)
            self._require_record(key)
            rule = {
                "ruleId": rule_id,
                "resourceType": resource_type,
                "resourceId": resource_id,
                "principalActorId": principal_actor_id,
                "principalOrganizationId": (principal_organization_id),
                "decision": decision,
                "updatedAt": "2026-01-01T00:00:00Z",
            }
            self.rules[
                (
                    resource_type,
                    resource_id,
                    principal_actor_id,
                    principal_organization_id,
                )
            ] = rule
            self._append_audit(
                key,
                actor_id,
                organization_id,
                "OP3",
                None,
                transaction_id,
            )
            return self._receipt(rule, transaction_id)

        if function == "EvaluateAccess":
            (
                resource_type,
                resource_id,
                actor_id,
                organization_id,
            ) = arguments
            result = self._access_result(
                resource_type,
                resource_id,
                actor_id,
                organization_id,
            )
            self._append_audit(
                (resource_type, resource_id),
                actor_id,
                organization_id,
                "OP4",
                result["decision"],
                transaction_id,
            )
            return self._receipt(result, transaction_id)

        raise AssertionError(f"unexpected submit: {function}")

    async def evaluate(
        self,
        function: str,
        arguments: tuple[str, ...],
    ) -> Any:
        if function == "GetRecordEvidence":
            key = (arguments[0], arguments[1])
            self._require_record(key)
            return GatewayReceipt(result=self.records[key])
        if function == "GetAccessDecision":
            return GatewayReceipt(result=self._access_result(*arguments))
        if function == "GetAudit":
            key = (arguments[0], arguments[1])
            self._require_record(key)
            return GatewayReceipt(result=list(self.audit.get(key, [])))
        raise AssertionError(f"unexpected evaluate: {function}")

    def _access_result(
        self,
        resource_type: str,
        resource_id: str,
        actor_id: str,
        organization_id: str,
    ) -> dict[str, Any]:
        key = (resource_type, resource_id)
        self._require_record(key)
        rule = self.rules.get(
            (
                resource_type,
                resource_id,
                actor_id,
                organization_id,
            )
        )
        return {
            "resourceType": resource_type,
            "resourceId": resource_id,
            "actorId": actor_id,
            "organizationId": organization_id,
            "decision": (rule["decision"] if rule is not None else "DENY"),
            "matchedRuleId": (rule["ruleId"] if rule is not None else None),
        }

    def _append_audit(
        self,
        key: tuple[str, str],
        actor_id: str,
        organization_id: str,
        action: str,
        decision: str | None,
        transaction_id: str,
    ) -> None:
        events = self.audit.setdefault(key, [])
        events.append(
            {
                "eventId": f"event-{transaction_id}",
                "sequence": len(events) + 1,
                "resourceType": key[0],
                "resourceId": key[1],
                "actorId": actor_id,
                "organizationId": organization_id,
                "action": action,
                "decision": decision,
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )

    def _require_record(self, key: tuple[str, str]) -> None:
        if key not in self.records:
            raise GatewayCallError(
                "missing",
                code="RECORD_NOT_FOUND",
                stage="evaluate",
            )

    @staticmethod
    def _receipt(
        result: Any,
        transaction_id: str,
    ) -> Any:
        return GatewayReceipt(
            result=result,
            transaction_id=transaction_id,
            commit_status="VALID",
            validation_code=0,
            block_number=1,
        )


def test_fabric_adapter_implements_protocol() -> None:
    adapter = FabricAdapter(
        payload_store=MemoryPayloadStore(),
        gateway=MemoryGateway(),
    )
    assert isinstance(adapter, ArchitectureAdapter)


def test_op1_through_op6_with_split_storage() -> None:
    async def scenario() -> None:
        payload_store = MemoryPayloadStore()
        gateway = MemoryGateway()
        administrator = _actor("administrator-001", "org-001")
        clinician = _actor("clinician-001", "org-002")
        resource = _resource("patient-fabric-unit-001")
        record = _record(resource.resource_id)

        async with FabricAdapter(
            payload_store=payload_store,
            gateway=gateway,
        ) as adapter:
            created = await adapter.create_record(
                CreateRecordRequest(
                    resource=resource,
                    actor=administrator,
                )
            )
            assert created.integrity.payload_hash == payload_sha256(
                resource.payload
            )
            assert (
                "payload"
                not in gateway.records[("Patient", record.resource_id)]
            )
            assert adapter.last_transaction_receipt is not None
            assert adapter.last_transaction_receipt.commit_status == "VALID"

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
                rule_id="rule-fabric-unit-001",
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
            assert integrity.matches is True

            payload_store.payloads[("Patient", record.resource_id)] = {
                "resourceType": "Patient",
                "id": record.resource_id,
                "active": False,
            }
            tampered = await adapter.verify_integrity(
                VerifyIntegrityRequest(record=record)
            )
            assert tampered.matches is False
            assert (
                tampered.authoritative_evidence.payload_hash
                == created.integrity.payload_hash
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

    asyncio.run(scenario())


def test_fabric_failure_rolls_back_staged_payload() -> None:
    async def scenario() -> None:
        payload_store = MemoryPayloadStore()
        gateway = MemoryGateway()
        gateway.fail_create = True
        resource = _resource("patient-fabric-rollback-001")

        async with FabricAdapter(
            payload_store=payload_store,
            gateway=gateway,
        ) as adapter:
            with pytest.raises(FABRIC_APP.FabricProtocolError):
                await adapter.create_record(
                    CreateRecordRequest(
                        resource=resource,
                        actor=_actor("administrator-001", "org-001"),
                    )
                )

        assert payload_store.payloads == {}

    asyncio.run(scenario())


def test_missing_records_map_to_common_error() -> None:
    async def scenario() -> None:
        adapter = FabricAdapter(
            payload_store=MemoryPayloadStore(),
            gateway=MemoryGateway(),
        )
        async with adapter:
            with pytest.raises(RecordNotFoundError):
                await adapter.verify_integrity(
                    VerifyIntegrityRequest(
                        record=_record("patient-fabric-missing")
                    )
                )

    asyncio.run(scenario())
