"""S3 / S3-compatible parquet helpers.

Uses s3fs so the same code works against AWS S3 or any S3-compatible
local store (Garage in dev). Polars → Arrow → parquet for the write
path.
"""

from __future__ import annotations

import io
import logging
import os

import polars as pl
import pyarrow.parquet as pq
import s3fs

logger = logging.getLogger(__name__)


def get_s3fs() -> s3fs.S3FileSystem:
    return s3fs.S3FileSystem(
        key=os.environ["S3_ACCESS_KEY"],
        secret=os.environ["S3_SECRET_KEY"],
        endpoint_url=f"http://{os.environ['S3_ENDPOINT']}",
        client_kwargs={"region_name": "us-east-1"},
    )


def get_bucket() -> str:
    return os.environ.get("S3_BUCKET", "sancha1090")


def write_parquet(df: pl.DataFrame, key: str) -> str:
    """Write a polars DataFrame to s3://{bucket}/{key} as snappy parquet.

    Idempotent: overwrites any existing object at the same key.
    Returns the full s3 URI.
    """
    fs = get_s3fs()
    bucket = get_bucket()
    path = f"{bucket}/{key}"

    table = df.to_arrow()
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)

    with fs.open(path, "wb") as f:
        f.write(buf.read())

    uri = f"s3://{path}"
    logger.info("Wrote %d rows to %s (%d bytes)", df.height, uri, buf.tell())
    return uri
