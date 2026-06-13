from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_states_table, raw_states_landed


@dag(
    dag_id="tableize_states",
    description="Append newly-landed raw state parquet into Iceberg bronze.opensky_states",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[raw_states_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "iceberg"],
)
def tableize_states():

    @task(outlets=[bronze_states_table])
    def load_pending_to_iceberg() -> dict:
        from datetime import datetime, timezone

        import polars as pl
        import pyarrow as pa
        from include import iceberg as ib
        from include import manifest
        from include.iceberg_rest import get_polaris_catalog
        from include.s3_helpers import garage_pyarrow_fs, read_pending_frames

        catalog = get_polaris_catalog()

        pending = manifest.pending_uris("bronze/states_raw")
        if not pending:
            return {"committed": 0, "rows": 0, "files": 0}

        uris = [r["object_uri"] for r in pending]
        fingerprint = manifest.batch_fingerprint(uris)
        table = catalog.load_table(ib.QUALIFIED)

        if manifest.already_appended(table, fingerprint):
            committed = manifest.mark_iceberg_committed(uris)
            return {"committed": committed, "rows": 0, "files": len(pending), "recovered": True}

        frames = read_pending_frames(garage_pyarrow_fs(), pending)

        df = pl.concat(frames, how="diagonal_relaxed")

        callsign_trim = pl.col("callsign").str.strip_chars()
        df = df.with_columns(
            pl.from_epoch(pl.col("snapshot_time"), time_unit="s")
                .dt.replace_time_zone("UTC").alias("snapshot_time"),
            pl.from_epoch(pl.col("last_contact"), time_unit="s")
                .dt.replace_time_zone("UTC").alias("last_contact"),
            pl.from_epoch(pl.col("time_position"), time_unit="s")
                .dt.replace_time_zone("UTC").alias("time_position"),
            pl.col("ingested_at").str.to_datetime(time_zone="UTC").alias("ingested_at"),
            pl.col("position_source").cast(pl.Int32),
            pl.when(callsign_trim == "").then(None).otherwise(callsign_trim).alias("callsign"),
            pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
        )

        columns = [f.name for f in ib.SCHEMA.fields]
        df = df.select(columns)

        arrow_table: pa.Table = df.to_arrow()
        table.append(
            arrow_table,
            snapshot_properties={
                "manifest_fingerprint": fingerprint,
                "uri_count": str(len(uris)),
            },
        )

        committed = manifest.mark_iceberg_committed(uris)

        return {"committed": committed, "rows": df.height, "files": len(pending)}

    load_pending_to_iceberg()


tableize_states()
