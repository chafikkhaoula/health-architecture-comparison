from __future__ import annotations

import os

from psycopg.conninfo import make_conninfo

from shared.http_api import create_operation_app

from .adapter import TraditionalPostgresAdapter


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _adapter_factory() -> TraditionalPostgresAdapter:
    conninfo = make_conninfo(
        host=os.environ.get(
            "TRADITIONAL_POSTGRES_HOST",
            "127.0.0.1",
        ),
        port=_required("TRADITIONAL_POSTGRES_PORT"),
        dbname=_required("POSTGRES_DB"),
        user=_required("APP_DB_USER"),
        password=_required("APP_DB_PASSWORD"),
    )
    return TraditionalPostgresAdapter(conninfo=conninfo)


app = create_operation_app(
    _adapter_factory,
    architecture="traditional",
)
