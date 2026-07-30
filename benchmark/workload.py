from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from math import ceil
from typing import Any

from pydantic import JsonValue

from benchmark.runner import PlannedRequest
from shared.data_generator import generate_fhir_resources
from shared.schemas import (
    AccessDecision,
    ActorContext,
    AuthorizationRule,
    CreateRecordRequest,
    EvaluateAccessRequest,
    FHIRResourceEnvelope,
    OperationId,
    RecordLocator,
    RetrieveAuditRequest,
    RetrieveRecordRequest,
    UpdateAuthorizationRequest,
    VerifyIntegrityRequest,
)


def _replace_references(
    value: JsonValue,
    replacements: dict[str, str],
) -> JsonValue:
    if isinstance(value, str):
        return replacements.get(value, value)

    if isinstance(value, list):
        return [
            _replace_references(item, replacements)
            for item in value
        ]

    if isinstance(value, dict):
        return {
            key: _replace_references(item, replacements)
            for key, item in value.items()
        }

    return value


def _namespace_resources(
    resources: Sequence[FHIRResourceEnvelope],
    namespace: str,
) -> tuple[FHIRResourceEnvelope, ...]:
    if not namespace or len(namespace) > 20:
        raise ValueError(
            "namespace must contain between 1 and 20 characters"
        )

    replacements = {
        f"{resource.resource_type.value}/{resource.resource_id}": (
            f"{resource.resource_type.value}/"
            f"{namespace}-{resource.resource_id}"
        )
        for resource in resources
    }

    namespaced = []
    for resource in resources:
        payload: dict[str, JsonValue] = deepcopy(resource.payload)
        payload["id"] = f"{namespace}-{resource.resource_id}"
        payload = {
            key: _replace_references(value, replacements)
            for key, value in payload.items()
        }
        namespaced.append(FHIRResourceEnvelope.from_payload(payload))

    return tuple(namespaced)


def build_workload_resources(
    request_count: int,
    *,
    seed: int,
    namespace: str,
) -> tuple[FHIRResourceEnvelope, ...]:
    """Build at least one unique deterministic resource per request."""
    if type(request_count) is not int or request_count < 1:
        raise ValueError("request_count must be a positive integer")

    generated = generate_fhir_resources(
        ceil(request_count / 4),
        seed=seed,
    )
    return _namespace_resources(generated, namespace)[
        :request_count
    ]


def _record(resource: FHIRResourceEnvelope) -> RecordLocator:
    return RecordLocator(
        resource_type=resource.resource_type,
        resource_id=resource.resource_id,
    )


def _request_payload(
    operation: OperationId,
    resource: FHIRResourceEnvelope,
    *,
    sequence: int,
    namespace: str,
    administrator: ActorContext,
    principal: ActorContext,
    access_decision: AccessDecision,
) -> Any:
    record = _record(resource)

    if operation is OperationId.CREATE_RECORD:
        return CreateRecordRequest(
            resource=resource,
            actor=administrator,
        )

    if operation is OperationId.RETRIEVE_RECORD:
        return RetrieveRecordRequest(
            record=record,
            actor=principal,
        )

    if operation is OperationId.UPDATE_AUTHORIZATION:
        return UpdateAuthorizationRequest(
            rule=AuthorizationRule(
                rule_id=f"rule-{namespace}-{sequence:06d}",
                record=record,
                principal=principal,
                decision=access_decision,
            ),
            actor=administrator,
        )

    if operation is OperationId.EVALUATE_ACCESS:
        return EvaluateAccessRequest(
            record=record,
            actor=principal,
        )

    if operation is OperationId.VERIFY_INTEGRITY:
        return VerifyIntegrityRequest(record=record)

    if operation is OperationId.RETRIEVE_AUDIT:
        return RetrieveAuditRequest(record=record)

    raise ValueError(f"unsupported operation: {operation}")


def build_operation_plan(
    operation: OperationId,
    resources: Sequence[FHIRResourceEnvelope],
    *,
    request_count: int,
    namespace: str,
    administrator: ActorContext,
    principal: ActorContext,
    access_decision: AccessDecision = AccessDecision.ALLOW,
) -> tuple[PlannedRequest, ...]:
    """Build deterministic measured requests for one operation."""
    if not isinstance(operation, OperationId):
        raise TypeError("operation must be an OperationId")

    if type(request_count) is not int or request_count < 1:
        raise ValueError("request_count must be a positive integer")

    if len(resources) < request_count:
        raise ValueError(
            "resources must contain at least request_count items"
        )

    expected_status = (
        201 if operation is OperationId.CREATE_RECORD else 200
    )
    expected_decision = (
        access_decision
        if operation is OperationId.EVALUATE_ACCESS
        else None
    )

    return tuple(
        PlannedRequest(
            request_id=(
                f"{namespace}-{operation.value}-{sequence:06d}"
            ),
            sequence=sequence,
            operation=operation,
            payload=_request_payload(
                operation,
                resources[sequence - 1],
                sequence=sequence,
                namespace=namespace,
                administrator=administrator,
                principal=principal,
                access_decision=access_decision,
            ),
            expected_http_status=expected_status,
            expected_authorization_decision=expected_decision,
        )
        for sequence in range(1, request_count + 1)
    )
