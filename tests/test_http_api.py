from __future__ import annotations

from fastapi.testclient import TestClient

from shared.contracts.errors import RecordAlreadyExistsError
from shared.http_api import create_operation_app
from shared.schemas import (
    ActorContext,
    CreateRecordRequest,
    CreateRecordResult,
    FHIRResourceEnvelope,
    IntegrityEvidence,
    RecordLocator,
)


def _request() -> CreateRecordRequest:
    resource = FHIRResourceEnvelope.from_payload(
        {
            "resourceType": "Patient",
            "id": "http-contract-patient",
            "active": True,
            "name": [
                {
                    "family": "Contract",
                    "given": ["HTTP"],
                }
            ],
        }
    )
    return CreateRecordRequest(
        resource=resource,
        actor=ActorContext(
            actor_id="administrator-http",
            organization_id="org-http",
        ),
    )


class ProbeAdapter:
    def __init__(self) -> None:
        self.entered = False
        self.create_calls = 0

    async def __aenter__(self) -> "ProbeAdapter":
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self.entered = False

    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        self.create_calls += 1
        return CreateRecordResult(
            record=RecordLocator(
                resource_type=request.resource.resource_type,
                resource_id=request.resource.resource_id,
            ),
            integrity=IntegrityEvidence(payload_hash="0" * 64),
        )

    async def retrieve_record(self, request: object) -> object:
        raise NotImplementedError

    async def update_authorization(self, request: object) -> object:
        raise NotImplementedError

    async def evaluate_access(self, request: object) -> object:
        raise NotImplementedError

    async def verify_integrity(self, request: object) -> object:
        raise NotImplementedError

    async def retrieve_audit(self, request: object) -> object:
        raise NotImplementedError


class ConflictAdapter(ProbeAdapter):
    async def create_record(
        self,
        request: CreateRecordRequest,
    ) -> CreateRecordResult:
        raise RecordAlreadyExistsError("duplicate record")


def test_http_lifespan_health_and_op1_contract() -> None:
    adapter = ProbeAdapter()
    app = create_operation_app(
        lambda: adapter,
        architecture="test",
    )

    with TestClient(app) as client:
        assert adapter.entered is True

        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "architecture": "test",
        }

        request = _request()
        response = client.post(
            "/v1/operations/OP1",
            json=request.model_dump(mode="json"),
        )

        assert response.status_code == 201
        assert response.json()["record"] == {
            "resource_type": "Patient",
            "resource_id": "http-contract-patient",
        }
        assert adapter.create_calls == 1

    assert adapter.entered is False


def test_http_rejects_unknown_resource_type() -> None:
    adapter = ProbeAdapter()
    app = create_operation_app(
        lambda: adapter,
        architecture="test",
    )

    payload = _request().model_dump(mode="json")
    payload["resource"]["resource_type"] = (
        "UnknownResourceType"
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/operations/OP1",
            json=payload,
        )

    assert response.status_code == 422
    assert adapter.create_calls == 0
    assert response.json()["detail"][0]["loc"][-1] == (
        "resource_type"
    )


def test_http_semantic_error_mapping() -> None:
    app = create_operation_app(
        ConflictAdapter,
        architecture="test",
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/operations/OP1",
            json=_request().model_dump(mode="json"),
        )

    assert response.status_code == 409
    assert response.json() == {
        "error_type": "RecordAlreadyExistsError",
        "detail": "record already exists",
    }


def test_openapi_exposes_all_frozen_operations() -> None:
    app = create_operation_app(
        ProbeAdapter,
        architecture="test",
    )

    paths = set(app.openapi()["paths"])

    assert {
        "/v1/operations/OP1",
        "/v1/operations/OP2",
        "/v1/operations/OP3",
        "/v1/operations/OP4",
        "/v1/operations/OP5",
        "/v1/operations/OP6",
    }.issubset(paths)
