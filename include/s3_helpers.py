"""S3 / S3-compatible parquet helpers.

Uses s3fs so the same code works against AWS S3 or any S3-compatible
local store (MinIO during migration, Garage after). Polars → Arrow →
parquet for the write path.

Env-var shim: prefer S3_* (Garage), fall back to MINIO_* (legacy).
The shim is removed in the cleanup commit once .env is migrated.
"""

from __future__ import annotations

import io
import logging
import os

import polars as pl
import pyarrow.parquet as pq
import s3fs

logger = logging.getLogger(__name__)


def _env_with_fallback(new_name: str, old_name: str) -> str | None:
    # Treat empty string as unset: docker compose substitutes "" for unset
    # vars from .env, which would otherwise mask the intended fallback.
    new_val = os.environ.get(new_name)
    if new_val:
        return new_val
    return os.environ.get(old_name) or None


def _get_endpoint() -> str:
    val = _env_with_fallback("S3_ENDPOINT", "MINIO_ENDPOINT")
    if not val:
        raise KeyError("S3_ENDPOINT (or fallback MINIO_ENDPOINT) must be set")
    return val


def _get_access_key() -> str:
    val = _env_with_fallback("S3_ACCESS_KEY", "MINIO_ACCESS_KEY")
    if not val:
        raise KeyError("S3_ACCESS_KEY (or fallback MINIO_ACCESS_KEY) must be set")
    return val


def _get_secret_key() -> str:
    val = _env_with_fallback("S3_SECRET_KEY", "MINIO_SECRET_KEY")
    if not val:
        raise KeyError("S3_SECRET_KEY (or fallback MINIO_SECRET_KEY) must be set")
    return val


def get_s3fs() -> s3fs.S3FileSystem:
    return s3fs.S3FileSystem(
        key=_get_access_key(),
        secret=_get_secret_key(),
        endpoint_url=f"http://{_get_endpoint()}",
        client_kwargs={"region_name": "us-east-1"},
    )


def get_bucket() -> str:
    return _env_with_fallback("S3_BUCKET", "MINIO_BUCKET") or "opensky"


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
