"""MinIO/S3 parquet helpers.

Uses s3fs so any code here works against real AWS S3 by changing
endpoint and credentials. Polars → Arrow → parquet for the write path.
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
    """Build an s3fs filesystem pointed at our MinIO endpoint.

    Reads MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY from env.
    """
    endpoint = os.environ["MINIO_ENDPOINT"]
    access_key = os.environ["MINIO_ACCESS_KEY"]
    secret_key = os.environ["MINIO_SECRET_KEY"]
    return s3fs.S3FileSystem(
        key=access_key,
        secret=secret_key,
        endpoint_url=f"http://{endpoint}",
        client_kwargs={"region_name": "us-east-1"},
    )


def get_bucket() -> str:
    """Read the bucket name from env, defaulting to 'opensky'."""
    return os.environ.get("MINIO_BUCKET", "opensky")


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