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
import pyarrow as pa
import pyarrow.parquet as pq
import s3fs
from pyarrow.fs import S3FileSystem

logger = logging.getLogger(__name__)


def get_s3fs() -> s3fs.S3FileSystem:
    return s3fs.S3FileSystem(
        key=os.environ["S3_ACCESS_KEY"],
        secret=os.environ["S3_SECRET_KEY"],
        endpoint_url=f"http://{os.environ['S3_ENDPOINT']}",
        client_kwargs={"region_name": "us-east-1"},
    )


# s3fs HEAD against Garage returns 400 without pre-warming; pyarrow doesn't.
def garage_pyarrow_fs() -> S3FileSystem:
    return S3FileSystem(
        endpoint_override=f"http://{os.environ['S3_ENDPOINT']}",
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        region="garage",
        scheme="http",
    )


def read_pending_frames(fs: S3FileSystem, pending: list[dict]) -> list[pl.DataFrame]:
    frames: list[pl.DataFrame] = []
    for row in pending:
        uri = row["object_uri"]
        if not uri.startswith("s3://"):
            raise ValueError(f"unexpected non-s3 manifest URI: {uri}")
        parquet_table = pq.read_table(uri[len("s3://"):], filesystem=fs)
        # polars from_arrow rejects non-string dict-encoded columns.
        decoded_columns = {}
        for name in parquet_table.column_names:
            col = parquet_table.column(name)
            if pa.types.is_dictionary(col.type):
                col = col.cast(col.type.value_type)
            decoded_columns[name] = col
        frames.append(pl.from_arrow(pa.table(decoded_columns)))
    return frames


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
