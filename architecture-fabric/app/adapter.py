from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from types import TracebackType
from typing import Any, Self

from shared.contracts.errors import (
    AccessDeniedError,
    AdapterError,
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from shared.hashing.canonical import payload_sha256
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

from .gateway import (
    FabricGatewayClient,
    GatewayCallError,
    GatewayReceipt,
    GatewayTransport,
)
from .payload_store import (
    FabricPostgresPayloadStore,
    PayloadStore,
)


class FabricProtocolError(AdapterError):
    """Raised when the bridge or chaincode violates the frozen contract."""


class FabricCommitError(AdapterError):
    """Raised when a Fabric transaction does not commit as VALID."""


_LAST_RECEIPT: ContextVar[GatewayReceipt | None] = ContextVar(
    "fabric_last_gateway_receipt",
    default=None,
)


class FabricAdapter:
    """Fabric/PostgreSQL implementation of the frozen OP1-OP6 contract."""

    def __init__(
        self,
        postgres_conninfo: str | None = None,
        gateway_url: str | None = None,
        *,
        payload_store: PayloadStore | None = None,
        gateway: GatewayTransport | None = None,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        pool_timeout_seconds: float = 30.0,
        gateway_timeout_seconds: float = 75.0,
    ) -> None:
        if payload_store is None:
            if postgres_conninfo is None:
                raise ValueError(
                    "postgres_conninfo is required without payload_store"
                )
            payload_store = FabricPostgresPayloadStore(
                postgres_conninfo,
                min_pool_size=min_pool_size,
                max_pool_size=max_pool_size,
                pool_timeout_seconds=pool_timeout_seconds,
            )
        if gateway is None:
            if gateway_url is None:
                raise ValueError("gateway_url is required without gateway")
            gateway = FabricGatewayClient(
                gateway_url,
                timeout_seconds=gateway_timeout_seconds,
                max_connections=max_pool_size * 2,
                max_keepalive_connections=max_pool_size,
            )

        self._payload_store = payload_store
        self._gateway = gateway

    async def open(self) -> None:
        await self._payload_store.open()
        try:
            await self._gateway.open()
        except Exception:
            await self._payload_store.close()
            raise

    async def close(self) -> None:
        try:
            await self._gateway.close()
        finally:
            await self._payload_store.close()

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

    @property
    def last_transaction_receipt(self) -> GatewayReceipt | None:
        """Return metadata for the latest write in the current async task."""
        return _LAST_RECEIPT.get()

    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        _LAST_RECEIPT.set(None)
        resource = request.resource
        record = RecordLocator(
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
        )
        digest = payload_sha256(resource.payload)
        integrity = IntegrityEvidence(payload_hash=digest)

        try:
            async with self._payload_store.stage_create(resource):
                receipt = await self._gateway.submit(
                    "CreateRecord",
                    (
                        record.resource_type.value,
                        record.resource_id,
                        digest,
                        request.actor.actor_id,
                        request.actor.organization_id,
                    ),
                )
                self._require_valid_commit(receipt)
                self._validate_record_evidence(
                    receipt.result,
                    record,
                    digest,
                )
                _LAST_RECEIPT.set(receipt)
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, record)

        return CreateRecordResult(
            record=record,
            integrity=integrity,
        )

    async def retrieve_record(
        self,
        request: RetrieveRecordRequest,
    ) -> RetrieveRecordResult:
        await self._get_record_evidence(request.record)

        access = await self._evaluate_without_audit(
            request.record,
            request.actor,
        )
        if access.decision is not AccessDecision.ALLOW:
            raise AccessDeniedError(
                f"access denied: "
                f"{request.record.resource_type.value}/"
                f"{request.record.resource_id}"
            )

        payload = await self._payload_store.get_payload(request.record)
        resource = FHIRResourceEnvelope(
            resource_type=request.record.resource_type,
            resource_id=request.record.resource_id,
            payload=payload,
        )
        return RetrieveRecordResult(resource=resource)

    async def update_authorization(
        self,
        request: UpdateAuthorizationRequest,
    ) -> UpdateAuthorizationResult:
        _LAST_RECEIPT.set(None)
        rule = request.rule
        try:
            receipt = await self._gateway.submit(
                "UpdateAuthorization",
                (
                    rule.rule_id,
                    rule.record.resource_type.value,
                    rule.record.resource_id,
                    rule.principal.actor_id,
                    rule.principal.organization_id,
                    rule.decision.value,
                    request.actor.actor_id,
                    request.actor.organization_id,
                ),
            )
            self._require_valid_commit(receipt)
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, rule.record)

        observed = self._authorization_rule_from_result(receipt.result)
        if observed != rule:
            raise FabricProtocolError(
                "chaincode returned a different authorization rule"
            )
        _LAST_RECEIPT.set(receipt)
        return UpdateAuthorizationResult(rule=rule)

    async def evaluate_access(
        self,
        request: EvaluateAccessRequest,
    ) -> EvaluateAccessResult:
        _LAST_RECEIPT.set(None)
        try:
            receipt = await self._gateway.submit(
                "EvaluateAccess",
                (
                    request.record.resource_type.value,
                    request.record.resource_id,
                    request.actor.actor_id,
                    request.actor.organization_id,
                ),
            )
            self._require_valid_commit(receipt)
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, request.record)

        result = self._access_result_from_gateway(receipt.result)
        if result.record != request.record or result.actor != request.actor:
            raise FabricProtocolError(
                "chaincode returned a mismatched access result"
            )
        _LAST_RECEIPT.set(receipt)
        return result

    async def verify_integrity(
        self,
        request: VerifyIntegrityRequest,
    ) -> VerifyIntegrityResult:
        evidence = await self._get_record_evidence(request.record)
        payload = await self._payload_store.get_payload(request.record)
        observed_hash = payload_sha256(payload)
        return VerifyIntegrityResult(
            record=request.record,
            authoritative_evidence=evidence,
            observed_hash=observed_hash,
            matches=(observed_hash == evidence.payload_hash),
        )

    async def retrieve_audit(
        self,
        request: RetrieveAuditRequest,
    ) -> RetrieveAuditResult:
        try:
            receipt = await self._gateway.evaluate(
                "GetAudit",
                (
                    request.record.resource_type.value,
                    request.record.resource_id,
                ),
            )
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, request.record)

        raw_events = receipt.result
        if not isinstance(raw_events, list):
            raise FabricProtocolError("GetAudit result must be a JSON array")
        events = tuple(
            self._audit_event_from_result(item) for item in raw_events
        )
        return RetrieveAuditResult(
            record=request.record,
            events=events,
        )

    async def _get_record_evidence(
        self,
        record: RecordLocator,
    ) -> IntegrityEvidence:
        try:
            receipt = await self._gateway.evaluate(
                "GetRecordEvidence",
                (
                    record.resource_type.value,
                    record.resource_id,
                ),
            )
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, record)

        return self._integrity_from_result(receipt.result, record)

    async def _evaluate_without_audit(
        self,
        record: RecordLocator,
        actor: ActorContext,
    ) -> EvaluateAccessResult:
        try:
            receipt = await self._gateway.evaluate(
                "GetAccessDecision",
                (
                    record.resource_type.value,
                    record.resource_id,
                    actor.actor_id,
                    actor.organization_id,
                ),
            )
        except GatewayCallError as exc:
            self._raise_gateway_error(exc, record)

        result = self._access_result_from_gateway(receipt.result)
        if result.record != record or result.actor != actor:
            raise FabricProtocolError(
                "chaincode returned a mismatched access result"
            )
        return result

    @staticmethod
    def _require_valid_commit(receipt: GatewayReceipt) -> None:
        if (
            receipt.transaction_id is None
            or receipt.commit_status != "VALID"
            or receipt.validation_code != 0
            or receipt.block_number is None
        ):
            raise FabricCommitError(
                "Fabric write did not return a confirmed VALID commit"
            )

    @staticmethod
    def _validate_record_evidence(
        value: Any,
        record: RecordLocator,
        expected_hash: str,
    ) -> None:
        data = FabricAdapter._require_dict(
            value,
            "CreateRecord result",
        )
        if (
            data.get("resourceType") != record.resource_type.value
            or data.get("resourceId") != record.resource_id
            or data.get("payloadHash") != expected_hash
            or data.get("algorithm") != "sha256"
            or data.get("canonicalization") != "RFC8785"
        ):
            raise FabricProtocolError(
                "CreateRecord returned mismatched integrity evidence"
            )

    @staticmethod
    def _integrity_from_result(
        value: Any,
        record: RecordLocator,
    ) -> IntegrityEvidence:
        data = FabricAdapter._require_dict(
            value,
            "GetRecordEvidence result",
        )
        if (
            data.get("resourceType") != record.resource_type.value
            or data.get("resourceId") != record.resource_id
        ):
            raise FabricProtocolError(
                "GetRecordEvidence returned a mismatched record"
            )
        try:
            return IntegrityEvidence(
                algorithm=data.get("algorithm"),
                canonicalization=data.get("canonicalization"),
                payload_hash=data.get("payloadHash"),
            )
        except Exception as exc:
            raise FabricProtocolError(
                "GetRecordEvidence returned invalid evidence"
            ) from exc

    @staticmethod
    def _authorization_rule_from_result(
        value: Any,
    ) -> AuthorizationRule:
        data = FabricAdapter._require_dict(
            value,
            "UpdateAuthorization result",
        )
        try:
            return AuthorizationRule(
                rule_id=data["ruleId"],
                record=RecordLocator(
                    resource_type=ResourceType(data["resourceType"]),
                    resource_id=data["resourceId"],
                ),
                principal=ActorContext(
                    actor_id=data["principalActorId"],
                    organization_id=(data["principalOrganizationId"]),
                ),
                decision=AccessDecision(data["decision"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FabricProtocolError(
                "UpdateAuthorization returned an invalid rule"
            ) from exc

    @staticmethod
    def _access_result_from_gateway(
        value: Any,
    ) -> EvaluateAccessResult:
        data = FabricAdapter._require_dict(
            value,
            "access decision result",
        )
        try:
            return EvaluateAccessResult(
                record=RecordLocator(
                    resource_type=ResourceType(data["resourceType"]),
                    resource_id=data["resourceId"],
                ),
                actor=ActorContext(
                    actor_id=data["actorId"],
                    organization_id=data["organizationId"],
                ),
                decision=AccessDecision(data["decision"]),
                matched_rule_id=data.get("matchedRuleId"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FabricProtocolError(
                "chaincode returned an invalid access result"
            ) from exc

    @staticmethod
    def _audit_event_from_result(value: Any) -> AuditEvent:
        data = FabricAdapter._require_dict(
            value,
            "audit event",
        )
        try:
            raw_decision = data.get("decision")
            return AuditEvent(
                event_id=data["eventId"],
                sequence=data["sequence"],
                record=RecordLocator(
                    resource_type=ResourceType(data["resourceType"]),
                    resource_id=data["resourceId"],
                ),
                actor=ActorContext(
                    actor_id=data["actorId"],
                    organization_id=data["organizationId"],
                ),
                action=OperationId(data["action"]),
                decision=(
                    AccessDecision(raw_decision)
                    if raw_decision is not None
                    else None
                ),
                timestamp=FabricAdapter._parse_timestamp(data["timestamp"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FabricProtocolError(
                "GetAudit returned an invalid event"
            ) from exc

    @staticmethod
    def _require_dict(
        value: Any,
        description: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise FabricProtocolError(f"{description} must be a JSON object")
        return value

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        if not isinstance(value, str):
            raise TypeError("timestamp must be a string")
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        timestamp = datetime.fromisoformat(normalized)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must include a UTC offset")
        return timestamp

    @staticmethod
    def _raise_gateway_error(
        error: GatewayCallError,
        record: RecordLocator,
    ) -> None:
        path = f"{record.resource_type.value}/{record.resource_id}"
        if error.code == "RECORD_ALREADY_EXISTS":
            raise RecordAlreadyExistsError(
                f"record already exists: {path}"
            ) from error
        if error.code == "RECORD_NOT_FOUND":
            raise RecordNotFoundError(f"record not found: {path}") from error
        if error.code == "INVALID_COMMIT":
            raise FabricCommitError(str(error)) from error
        raise FabricProtocolError(
            f"Fabric Gateway {error.stage} failure ({error.code}): {error}"
        ) from error
