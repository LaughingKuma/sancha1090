from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_aircraft_db_table

# Static download, no API credits (verified 2026-06-10); refreshed ~weekly upstream.
AIRCRAFT_DB_URL = "https://s3.opensky-network.org/data-samples/metadata/aircraftDatabase.csv"


@dag(
    dag_id="ingest_aircraft_db",
    description="Weekly OpenSky aircraft registry CSV → bronze.aircraft_db (identity layer)",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    # 02:00 JST Monday, off-peak; ahead of refresh_risingwave_dims (Mon 05:15 UTC).
    schedule="0 17 * * 0",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "bronze", "iceberg", "v5"],
)
def ingest_aircraft_db():

    @task
    def download_and_land(**context) -> dict:
        import io

        import httpx
        import polars as pl
        from include.flights_iceberg import AIRCRAFT_DB_CSV_COLUMNS
        from include.s3_helpers import write_parquet
        from include import manifest

        buf = io.BytesIO()
        with httpx.stream("GET", AIRCRAFT_DB_URL, timeout=300.0, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                buf.write(chunk)
        buf.seek(0)

        df = pl.read_csv(buf, columns=AIRCRAFT_DB_CSV_COLUMNS, infer_schema_length=0)

        # Registry rows without an airframe address can never join anything.
        df = df.filter(pl.col("icao24").str.strip_chars() != "")

        # Manual runs carry no logical_date in Airflow 3; run_after is always set.
        logical = context.get("logical_date") or context["dag_run"].run_after
        df = df.with_columns(
            pl.col("icao24").str.to_lowercase(),
            pl.lit(logical.strftime("%Y-%m-%d")).alias("as_of_date"),
            pl.lit(logical.isoformat()).alias("ingested_at"),
        )

        key = f"bronze/aircraft_db_raw/dt={logical.strftime('%Y-%m-%d')}/aircraft_db.parquet"
        uri = write_parquet(df, key)
        manifest.record_load(
            object_uri=uri,
            snapshot_min=None,
            snapshot_max=None,
            row_count=df.height,
        )
        return {"rows": df.height, "uri": uri}

    @task(outlets=[bronze_aircraft_db_table])
    def tableize(**_context) -> dict:
        from datetime import datetime, timezone

        import polars as pl
        from include import flights_iceberg as fib
        from include import manifest
        from include.s3_helpers import garage_pyarrow_fs, read_pending_frames

        table = fib.ensure_aircraft_db_table()

        pending = manifest.pending_uris("bronze/aircraft_db_raw")
        if not pending:
            return {"committed": 0, "rows": 0, "files": 0}

        uris = [r["object_uri"] for r in pending]
        fingerprint = manifest.batch_fingerprint(uris)

        if manifest.already_appended(table, fingerprint):
            committed = manifest.mark_iceberg_committed(uris)
            return {"committed": committed, "rows": 0, "files": len(pending), "recovered": True}

        frames = read_pending_frames(garage_pyarrow_fs(), pending)

        df = pl.concat(frames, how="diagonal_relaxed")

        empty_to_null = [
            pl.when(pl.col(c).str.strip_chars() == "").then(None)
              .otherwise(pl.col(c).str.strip_chars()).alias(c)
            for c in fib.AIRCRAFT_DB_CSV_COLUMNS
        ]
        df = df.with_columns(empty_to_null).with_columns(
            pl.col("as_of_date").str.to_date(),
            pl.col("ingested_at").str.to_datetime(time_zone="UTC"),
            pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
        )

        columns = [f.name for f in fib.AIRCRAFT_DB_SCHEMA.fields]
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

    download_and_land() >> tableize()


ingest_aircraft_db()
