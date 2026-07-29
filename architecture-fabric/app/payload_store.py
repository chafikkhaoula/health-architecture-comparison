from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import TracebackType
from typing import Any, Protocol, Self

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from shared.contracts.errors import (
    RecordAlreadyExistsError,
    RecordNotFoundError,
)
from shared.schemas import FHIRResourceEnvelope, RecordLocator


class PayloadStore(Protocol):
    async def open(self) -> None: ...

    async def close(self) -> None: ...

    def stage_create(
        self,
        resource: FHIRResourceEnvelope,
    ) -> AbstractAsyncContextManager[None]: ...

    async def get_payload(
        self,
        record: RecordLocator,
    ) -> dict[str, Any]: ...


class FabricPostgresPayloadStore:
    """PostgreSQL store containing clinical payloads only."""

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
                "max_pool_size must be greater than or equal to min_pool_size"
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
            name="fabric-postgres-payloads",
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

    @asynccontextmanager
    async def stage_create(
        self,
        resource: FHIRResourceEnvelope,
    ) -> AsyncIterator[None]:
        """
        Hold the insert transaction open until Fabric confirms commit.

        A Fabric failure therefore rolls the off-chain insertion back.
        Fabric and PostgreSQL cannot provide a distributed atomic commit;
        a database commit failure after a VALID Fabric commit is surfaced
        to the caller and must be retained as consistency evidence.
        """
        try:
            async with (
                self._pool.connection() as connection,
                connection.transaction(),
            ):
                await connection.execute(
                    """
                    INSERT INTO clinical_payloads (
                        resource_type,
                        resource_id,
                        payload
                    )
                    VALUES (%s, %s, %s)
                    """,
                    (
                        resource.resource_type.value,
                        resource.resource_id,
                        Jsonb(resource.payload),
                    ),
                )
                yield
        except UniqueViolation as exc:
            if exc.diag.constraint_name == "clinical_payloads_pk":
                raise RecordAlreadyExistsError(
                    f"record already exists: "
                    f"{resource.resource_type.value}/"
                    f"{resource.resource_id}"
                ) from exc
            raise

    async def get_payload(
        self,
        record: RecordLocator,
    ) -> dict[str, Any]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT payload
                FROM clinical_payloads
                WHERE resource_type = %s
                  AND resource_id = %s
                """,
                (
                    record.resource_type.value,
                    record.resource_id,
                ),
            )
            row = await cursor.fetchone()

        if row is None:
            raise RecordNotFoundError(
                f"record not found: "
                f"{record.resource_type.value}/{record.resource_id}"
            )
        payload = row["payload"]
        if not isinstance(payload, dict):
            raise TypeError("stored clinical payload is not an object")
        return payload
