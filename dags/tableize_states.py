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
    tags=["opensky", "bronze", "iceberg", "stage-12"],
)
def tableize_states():

    @task(outlets=[bronze_states_table])
    def load_pending_to_iceberg() -> dict:
        import hashlib
        import os
        from datetime import datetime, timezone

        import polars as pl
        import pyarrow as pa
        import pyarrow.parquet as pq
        from pyarrow.fs import S3FileSystem
        from include import iceberg as ib
        from include import manifest

        catalog = ib.get_catalog()
        ib.ensure_namespace_and_table(catalog)

        pending = manifest.pending_uris()
        if not pending:
            return {"committed": 0, "rows": 0, "files": 0}

        uris = [r["object_uri"] for r in pending]
        fingerprint = hashlib.sha256("\n".join(sorted(uris)).encode()).hexdigest()
        table = catalog.load_table(ib.QUALIFIED)

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
            parquet_table = pq.read_table(path, filesystem=fs)
            # polars from_arrow rejects non-string dict-encoded columns.
            decoded_columns = {}
            for name in parquet_table.column_names:
                col = parquet_table.column(name)
                if pa.types.is_dictionary(col.type):
                    col = col.cast(col.type.value_type)
                decoded_columns[name] = col
            frames.append(pl.from_arrow(pa.table(decoded_columns)))

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
