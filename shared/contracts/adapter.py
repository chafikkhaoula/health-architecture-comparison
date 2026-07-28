from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from shared.schemas import (
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    OperationId,
    RetrieveAuditRequest,
    RetrieveAuditResult,
    RetrieveRecordRequest,
    RetrieveRecordResult,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)

OPERATION_METHODS: Mapping[OperationId, str] = MappingProxyType(
    {
        OperationId.CREATE_RECORD: "create_record",
        OperationId.RETRIEVE_RECORD: "retrieve_record",
        OperationId.UPDATE_AUTHORIZATION: "update_authorization",
        OperationId.EVALUATE_ACCESS: "evaluate_access",
        OperationId.VERIFY_INTEGRITY: "verify_integrity",
        OperationId.RETRIEVE_AUDIT: "retrieve_audit",
    }
)


@runtime_checkable
class ArchitectureAdapter(Protocol):
    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        ...

    async def retrieve_record(
        self,
        request: RetrieveRecordRequest,
    ) -> RetrieveRecordResult:
        ...

    async def update_authorization(
        self,
        request: UpdateAuthorizationRequest,
    ) -> UpdateAuthorizationResult:
        ...

    async def evaluate_access(
        self,
        request: EvaluateAccessRequest,
    ) -> EvaluateAccessResult:
        ...

    async def verify_integrity(
        self,
        request: VerifyIntegrityRequest,
    ) -> VerifyIntegrityResult:
        ...

    async def retrieve_audit(
        self,
        request: RetrieveAuditRequest,
    ) -> RetrieveAuditResult:
        ...
