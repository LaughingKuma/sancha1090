from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import raw_states_landed


@dag(
    dag_id="backfill_from_buffer",
    description="Sync VPS-collected parquets from R2 buffer into Garage; register in ingestion_manifest",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "backfill", "vps", "manual"],
)
def backfill_from_buffer():

    @task(outlets=[raw_states_landed])
    def sync_r2_to_garage() -> dict:
        import os
        import sqlalchemy as sa
        from pyarrow.fs import S3FileSystem, FileSelector
        from include import manifest
        from include.db import analytics_engine
        from include.s3_helpers import get_bucket, get_s3fs

        r2 = S3FileSystem(
            endpoint_override=os.environ["R2_ENDPOINT"],
            access_key=os.environ["R2_ACCESS_KEY"],
            secret_key=os.environ["R2_SECRET"],
            region=os.environ.get("R2_REGION", "auto"),
            scheme="https",
        )
        r2_bucket = os.environ.get("R2_BUCKET", "opensky-vps-buffer")

        manifest.ensure_table()
        eng = analytics_engine()
        with eng.begin() as conn:
            existing = {
                row[0] for row in conn.execute(
                    sa.text("SELECT object_uri FROM public.ingestion_manifest")
                ).fetchall()
            }

        prefix = f"{r2_bucket}/bronze/states_raw"
        try:
            entries = r2.get_file_info(FileSelector(prefix, recursive=True))
        except Exception as exc:
            print(f"r2 list failed: {exc}")
            return {"copied": 0, "skipped": 0}

        garage_fs = get_s3fs()
        garage_bucket = get_bucket()

        insert = sa.text(
            """
            INSERT INTO public.ingestion_manifest (object_uri)
            VALUES (:uri)
            ON CONFLICT (object_uri) DO NOTHING
            """
        )

        copied = 0
        skipped = 0
        with eng.begin() as conn:
            for entry in entries:
                if not entry.path.endswith(".parquet"):
                    continue

                # Mirror the R2 path under the Garage bucket so the manifest URIs stay consistent.
                relative = entry.path[len(f"{r2_bucket}/"):]
                target_uri = f"s3://{garage_bucket}/{relative}"
                if target_uri in existing:
                    skipped += 1
                    continue

                with r2.open_input_stream(entry.path) as src:
                    data = src.read()
                target_key = f"{garage_bucket}/{relative}"
                with garage_fs.open(target_key, "wb") as dst:
                    dst.write(data)

                conn.execute(insert, {"uri": target_uri})
                copied += 1

        print(f"copied={copied} skipped={skipped}")
        return {"copied": copied, "skipped": skipped}

    sync_r2_to_garage()


backfill_from_buffer()
