from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from inspect import isawaitable
from typing import Annotated, Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator

from shared.contracts import errors as contract_errors
from shared.contracts.adapter import ArchitectureAdapter
from shared.schemas import (
    CreateRecordRequest,
    CreateRecordResult,
    EvaluateAccessRequest,
    EvaluateAccessResult,
    RetrieveAuditRequest,
    RetrieveAuditResult,
    RetrieveRecordRequest,
    RetrieveRecordResult,
    UpdateAuthorizationRequest,
    UpdateAuthorizationResult,
    VerifyIntegrityRequest,
    VerifyIntegrityResult,
)

AdapterFactory = Callable[[], ArchitectureAdapter]


def _http_request(model: type[Any]) -> Any:
    """Decode JSON-native values into strict domain-model types."""
    return Annotated[
        model,
        BeforeValidator(
            lambda value: model.model_validate(
                value,
                strict=False,
            )
        ),
    ]


CreateRecordHttpRequest = _http_request(CreateRecordRequest)
RetrieveRecordHttpRequest = _http_request(RetrieveRecordRequest)
UpdateAuthorizationHttpRequest = _http_request(
    UpdateAuthorizationRequest
)
EvaluateAccessHttpRequest = _http_request(EvaluateAccessRequest)
VerifyIntegrityHttpRequest = _http_request(VerifyIntegrityRequest)
RetrieveAuditHttpRequest = _http_request(RetrieveAuditRequest)


def _receipt_value(receipt: object, *names: str) -> object | None:
    if isinstance(receipt, Mapping):
        for name in names:
            value = receipt.get(name)
            if value is not None:
                return value

    for name in names:
        value = getattr(receipt, name, None)
        if value is not None:
            return value

    return None


def _attach_receipt_headers(
    response: Response,
    adapter: ArchitectureAdapter,
) -> None:
    receipt: Any = getattr(
        adapter,
        "last_transaction_receipt",
        None,
    )

    if callable(receipt):
        receipt = receipt()

    if receipt is None:
        return

    transaction_id = _receipt_value(
        receipt,
        "transaction_id",
        "transactionId",
        "tx_id",
        "txId",
    )
    validation_code = _receipt_value(
        receipt,
        "validation_code",
        "validationCode",
        "commit_status",
        "commitStatus",
        "status",
    )

    if transaction_id is not None:
        response.headers["X-Fabric-Tx-Id"] = str(transaction_id)

    if validation_code is not None:
        response.headers["X-Fabric-Validation-Code"] = str(
            validation_code
        )


def _error_handler(
    status_code: int,
    public_detail: str,
) -> Callable[..., Any]:
    async def handler(
        _: Request,
        exc: Exception,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "error_type": type(exc).__name__,
                "detail": public_detail,
            },
        )

    return handler


def _register_error_handlers(app: FastAPI) -> None:
    specifications = {
        "RecordAlreadyExistsError": (
            status.HTTP_409_CONFLICT,
            "record already exists",
        ),
        "RecordNotFoundError": (
            status.HTTP_404_NOT_FOUND,
            "record not found",
        ),
        "AccessDeniedError": (
            status.HTTP_403_FORBIDDEN,
            "access denied",
        ),
        "AdapterError": (
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "backend operation failed",
        ),
    }

    for class_name, (
        status_code,
        public_detail,
    ) in specifications.items():
        exception_class = getattr(
            contract_errors,
            class_name,
            None,
        )

        if isinstance(exception_class, type):
            app.add_exception_handler(
                exception_class,
                _error_handler(status_code, public_detail),
            )


def create_operation_app(
    adapter_factory: AdapterFactory,
    *,
    architecture: str,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        adapter = adapter_factory()
        active_adapter = adapter

        enter = getattr(adapter, "__aenter__", None)
        exit_method = getattr(adapter, "__aexit__", None)

        if callable(enter):
            entered = enter()
            if isawaitable(entered):
                entered = await entered
            if entered is not None:
                active_adapter = entered

        app.state.adapter = active_adapter

        try:
            yield
        finally:
            if callable(exit_method):
                exited = exit_method(None, None, None)
                if isawaitable(exited):
                    await exited
            else:
                close = getattr(adapter, "close", None)
                if callable(close):
                    closed = close()
                    if isawaitable(closed):
                        await closed

    app = FastAPI(
        title=f"Health Architecture {architecture.title()} API",
        version="1.0.0",
        lifespan=lifespan,
    )
    _register_error_handlers(app)

    def adapter_from(request: Request) -> ArchitectureAdapter:
        return request.app.state.adapter

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "architecture": architecture,
        }

    @app.post(
        "/v1/operations/OP1",
        response_model=CreateRecordResult,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_record(
        payload: CreateRecordHttpRequest,
        request: Request,
        response: Response,
    ) -> CreateRecordResult:
        adapter = adapter_from(request)
        result = await adapter.create_record(payload)
        _attach_receipt_headers(response, adapter)
        return result

    @app.post(
        "/v1/operations/OP2",
        response_model=RetrieveRecordResult,
    )
    async def retrieve_record(
        payload: RetrieveRecordHttpRequest,
        request: Request,
        response: Response,
    ) -> RetrieveRecordResult:
        adapter = adapter_from(request)
        result = await adapter.retrieve_record(payload)
        _attach_receipt_headers(response, adapter)
        return result

    @app.post(
        "/v1/operations/OP3",
        response_model=UpdateAuthorizationResult,
    )
    async def update_authorization(
        payload: UpdateAuthorizationHttpRequest,
        request: Request,
        response: Response,
    ) -> UpdateAuthorizationResult:
        adapter = adapter_from(request)
        result = await adapter.update_authorization(payload)
        _attach_receipt_headers(response, adapter)
        return result

    @app.post(
        "/v1/operations/OP4",
        response_model=EvaluateAccessResult,
    )
    async def evaluate_access(
        payload: EvaluateAccessHttpRequest,
        request: Request,
        response: Response,
    ) -> EvaluateAccessResult:
        adapter = adapter_from(request)
        result = await adapter.evaluate_access(payload)
        _attach_receipt_headers(response, adapter)
        return result

    @app.post(
        "/v1/operations/OP5",
        response_model=VerifyIntegrityResult,
    )
    async def verify_integrity(
        payload: VerifyIntegrityHttpRequest,
        request: Request,
        response: Response,
    ) -> VerifyIntegrityResult:
        adapter = adapter_from(request)
        result = await adapter.verify_integrity(payload)
        _attach_receipt_headers(response, adapter)
        return result

    @app.post(
        "/v1/operations/OP6",
        response_model=RetrieveAuditResult,
    )
    async def retrieve_audit(
        payload: RetrieveAuditHttpRequest,
        request: Request,
        response: Response,
    ) -> RetrieveAuditResult:
        adapter = adapter_from(request)
        result = await adapter.retrieve_audit(payload)
        _attach_receipt_headers(response, adapter)
        return result

    return app
