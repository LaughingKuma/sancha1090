from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import raw_states_landed


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

    @task
    def load_pending_to_iceberg() -> dict:
        import os
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
            assert uri.startswith("s3://")
            path = uri[len("s3://"):]
            table = pq.read_table(path, filesystem=fs)
            # polars from_arrow rejects non-string dict-encoded columns.
            decoded_columns = {}
            for name in table.column_names:
                col = table.column(name)
                if pa.types.is_dictionary(col.type):
                    col = col.cast(col.type.value_type)
                decoded_columns[name] = col
            table = pa.table(decoded_columns)
            frames.append(pl.from_arrow(table))

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
        )

        columns = [f.name for f in ib.SCHEMA.fields]
        df = df.select(columns)

        arrow_table: pa.Table = df.to_arrow()
        table = catalog.load_table(ib.QUALIFIED)
        table.append(arrow_table)

        uris = [r["object_uri"] for r in pending]
        committed = manifest.mark_iceberg_committed(uris)

        return {"committed": committed, "rows": df.height, "files": len(pending)}

    load_pending_to_iceberg()


tableize_states()
