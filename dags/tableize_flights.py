from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_flights_table, raw_flights_landed


@dag(
    dag_id="tableize_flights",
    description="Append newly-landed raw flights parquet into Iceberg bronze.opensky_flights",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    schedule=[raw_flights_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "iceberg", "v5"],
)
def tableize_flights():

    @task(outlets=[bronze_flights_table])
    def load_pending_to_iceberg() -> dict:
        import hashlib
        import os
        from datetime import datetime, timezone

        import polars as pl
        import pyarrow.parquet as pq
        from pyarrow.fs import S3FileSystem
        from include import flights_iceberg as fib
        from include import manifest

        table = fib.ensure_flights_table()

        pending = manifest.pending_uris("bronze/flights_raw")
        if not pending:
            return {"committed": 0, "rows": 0, "files": 0}

        uris = [r["object_uri"] for r in pending]
        fingerprint = hashlib.sha256("\n".join(sorted(uris)).encode()).hexdigest()

        # Crash-recovery: if the last commit's snapshot already carries this batch's
        # fingerprint, the Iceberg append succeeded on a prior attempt that died before
        # marking the manifest. Skip the append; just reconcile the manifest.
        current = table.current_snapshot()
        if current and current.summary and current.summary.additional_properties.get("manifest_fingerprint") == fingerprint:
            committed = manifest.mark_iceberg_committed(uris)
            return {"committed": committed, "rows": 0, "files": len(pending), "recovered": True}

        # s3fs HEAD against Garage returns 400 without pre-warming; pyarrow doesn't.
        fs = S3FileSystem(
            endpoint_override=f"http://{os.environ['S3_ENDPOINT']}",
            access_key=os.environ["S3_ACCESS_KEY"],
            secret_key=os.environ["S3_SECRET_KEY"],
            region="garage",
            scheme="http",
        )

        frames: list[pl.DataFrame] = []
        for row in pending:
            uri = row["object_uri"]
            if not uri.startswith("s3://"):
                raise ValueError(f"unexpected non-s3 manifest URI: {uri}")
            path = uri[len("s3://"):]
            frames.append(pl.from_arrow(pq.read_table(path, filesystem=fs)))

        df = pl.concat(frames, how="diagonal_relaxed")

        callsign_trim = pl.col("callsign").str.strip_chars()
        df = df.with_columns(
            pl.from_epoch(pl.col("first_seen"), time_unit="s")
                .dt.replace_time_zone("UTC").alias("first_seen"),
            pl.from_epoch(pl.col("last_seen"), time_unit="s")
                .dt.replace_time_zone("UTC").alias("last_seen"),
            (pl.col("last_seen") - pl.col("first_seen")).cast(pl.Int32)
                .alias("flight_duration_seconds"),
            pl.col("ingested_at").str.to_datetime(time_zone="UTC").alias("ingested_at"),
            pl.when(callsign_trim == "").then(None).otherwise(callsign_trim).alias("callsign"),
            pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
        )

        columns = [f.name for f in fib.FLIGHTS_SCHEMA.fields]
        df = df.select(columns)

        table.append(
            df.to_arrow(),
            snapshot_properties={
                "manifest_fingerprint": fingerprint,
                "uri_count": str(len(uris)),
            },
        )

        committed = manifest.mark_iceberg_committed(uris)

        return {"committed": committed, "rows": df.height, "files": len(pending)}

    load_pending_to_iceberg()


tableize_flights()
