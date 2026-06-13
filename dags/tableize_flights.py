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
        from datetime import datetime, timezone

        import polars as pl
        from include import flights_iceberg as fib
        from include import manifest
        from include.s3_helpers import garage_pyarrow_fs, read_pending_frames

        table = fib.ensure_flights_table()

        pending = manifest.pending_uris("bronze/flights_raw")
        if not pending:
            return {"committed": 0, "rows": 0, "files": 0}

        uris = [r["object_uri"] for r in pending]
        fingerprint = manifest.batch_fingerprint(uris)

        if manifest.already_appended(table, fingerprint):
            committed = manifest.mark_iceberg_committed(uris)
            return {"committed": committed, "rows": 0, "files": len(pending), "recovered": True}

        frames = read_pending_frames(garage_pyarrow_fs(), pending)

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
