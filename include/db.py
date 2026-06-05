from __future__ import annotations

import os

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
