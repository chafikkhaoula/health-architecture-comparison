from __future__ import annotations

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self

import httpx


@dataclass(frozen=True, slots=True)
class GatewayReceipt:
    result: Any
    transaction_id: str | None = None
    commit_status: str | None = None
    validation_code: int | None = None
    block_number: int | None = None


class GatewayCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        stage: str,
        transaction_id: str | None = None,
        validation_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.transaction_id = transaction_id
        self.validation_code = validation_code


class GatewayTransport(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def evaluate(
        self,
        function: str,
        arguments: tuple[str, ...],
    ) -> GatewayReceipt: ...

    async def submit(
        self,
        function: str,
        arguments: tuple[str, ...],
        *,
        endorsing_organizations: tuple[str, ...] = (),
    ) -> GatewayReceipt: ...


class FabricGatewayClient:
    """Persistent HTTP client for the local Go Fabric Gateway bridge."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 75.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        if not 1 <= max_keepalive_connections <= max_connections:
            raise ValueError(
                "max_keepalive_connections must be between 1 "
                "and max_connections"
            )

        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            limits=self._limits,
        )
        await self._request("GET", "/healthz")

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

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

    async def evaluate(
        self,
        function: str,
        arguments: tuple[str, ...],
    ) -> GatewayReceipt:
        body = await self._request(
            "POST",
            "/v1/evaluate",
            json={
                "function": function,
                "arguments": list(arguments),
            },
        )
        return self._receipt_from_body(body)

    async def submit(
        self,
        function: str,
        arguments: tuple[str, ...],
        *,
        endorsing_organizations: tuple[str, ...] = (),
    ) -> GatewayReceipt:
        body = await self._request(
            "POST",
            "/v1/submit",
            json={
                "function": function,
                "arguments": list(arguments),
                "endorsing_organizations": list(endorsing_organizations),
            },
        )
        return self._receipt_from_body(body)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        client = self._client
        if client is None:
            raise RuntimeError("FabricGatewayClient is not open")

        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GatewayCallError(
                f"gateway bridge unavailable: {exc}",
                code="BRIDGE_UNAVAILABLE",
                stage="http",
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise GatewayCallError(
                "gateway bridge returned non-JSON content",
                code="INVALID_BRIDGE_RESPONSE",
                stage="http",
            ) from exc

        if not isinstance(body, dict):
            raise GatewayCallError(
                "gateway bridge returned an invalid JSON object",
                code="INVALID_BRIDGE_RESPONSE",
                stage="http",
            )

        if response.is_error:
            raw_error = body.get("error")
            error = raw_error if isinstance(raw_error, dict) else {}
            raise GatewayCallError(
                str(error.get("message", "Fabric Gateway call failed")),
                code=str(error.get("code", "GATEWAY_ERROR")),
                stage=str(error.get("stage", "unknown")),
                transaction_id=self._optional_string(
                    error.get("transaction_id")
                ),
                validation_code=self._optional_int(
                    error.get("validation_code")
                ),
            )

        return body

    @classmethod
    def _receipt_from_body(
        cls,
        body: dict[str, Any],
    ) -> GatewayReceipt:
        return GatewayReceipt(
            result=body.get("result"),
            transaction_id=cls._optional_string(body.get("transaction_id")),
            commit_status=cls._optional_string(body.get("commit_status")),
            validation_code=cls._optional_int(body.get("validation_code")),
            block_number=cls._optional_int(body.get("block_number")),
        )

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return value if isinstance(value, int) else None
