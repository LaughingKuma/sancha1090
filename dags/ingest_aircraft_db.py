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
    tags=["sancha1090", "bronze", "v5"],
)
def ingest_aircraft_db():

    @task
    def download_and_land(**context) -> dict:
        import io

        import httpx
        import polars as pl
        from include.bronze_transforms import AIRCRAFT_DB_CSV_COLUMNS
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
    def load_to_clickhouse() -> dict:
        # Non-destructive reload from the Garage Parquet glob (the blank-cell -> NULL coercion + date parsing
        # live in backfill_aircraft_db's SQL). Raise on failure so a broken reload reds the weekly run rather
        # than silently leaving the registry stale (download_and_land already landed a fresh snapshot, so an
        # ok=False here means the CH load genuinely failed, not an empty glob).
        from include.clickhouse import backfill_aircraft_db

        result = backfill_aircraft_db()
        if not result.get("ok"):
            raise RuntimeError(f"CH aircraft_db load failed: {result}")
        return result

    download_and_land() >> load_to_clickhouse()


ingest_aircraft_db()
