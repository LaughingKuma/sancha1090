from __future__ import annotations

import os
import sys

import sqlalchemy as sa

sys.path.insert(0, "/opt/airflow")
from include import manifest
from include.s3_helpers import get_bucket, get_s3fs


def main() -> int:
    fs = get_s3fs()
    bucket = get_bucket()
    prefix = f"{bucket}/bronze/states/"

    files = fs.glob(f"{prefix}**/*.parquet")
    print(f"found {len(files)} legacy files under bronze/states/")
    if not files:
        return 0

    manifest.ensure_table()
    eng = manifest._engine()
    stmt = sa.text(
        """
        INSERT INTO public.ingestion_manifest (object_uri)
        VALUES (:uri)
        ON CONFLICT (object_uri) DO NOTHING
        """
    )
    with eng.begin() as conn:
        for path in files:
            uri = f"s3://{path}"
            conn.execute(stmt, {"uri": uri})

    with eng.begin() as conn:
        pending = conn.execute(sa.text(
            "SELECT count(*) FROM public.ingestion_manifest WHERE iceberg_committed_at IS NULL"
        )).scalar()
    print(f"pending manifest rows: {pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
