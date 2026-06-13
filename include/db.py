from __future__ import annotations

import os

import psycopg2
import sqlalchemy as sa
from sqlalchemy.engine import URL


def analytics_url() -> URL:
    return URL.create(
        "postgresql+psycopg2",
        username=os.environ["ANALYTICS_PG_USER"],
        password=os.environ["ANALYTICS_PG_PASSWORD"],
        host=os.environ["ANALYTICS_PG_HOST"],
        port=int(os.environ.get("ANALYTICS_PG_PORT", "5432")),
        database=os.environ["ANALYTICS_PG_DB"],
    )


def analytics_engine() -> sa.Engine:
    return sa.create_engine(analytics_url())


def rw_connect() -> psycopg2.extensions.connection:
    conn = psycopg2.connect(
        host=os.environ.get("RISINGWAVE_HOST", "risingwave"),
        port=int(os.environ.get("RISINGWAVE_PORT", "4566")),
        user="root",
        dbname="dev",
    )
    conn.autocommit = True
    return conn


def trino_connect(schema: str):
    # lazy: a module-level import would force trino on every db.py importer; tests/backfill treat it as optional
    import trino

    return trino.dbapi.connect(
        host=os.environ.get("TRINO_HOST", "trino-coordinator"),
        port=int(os.environ.get("TRINO_PORT", "8080")),
        user=os.environ.get("TRINO_USER", "airflow"),
        catalog="iceberg",
        schema=schema,
    )
