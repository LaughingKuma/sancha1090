from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_adsbx_db_table

# Static public download, no auth (verified 2026-07-16); ADSBx refreshes daily, we snapshot weekly.
ADSBX_DB_URL = "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz"

# ~81% of the measured 2026-07-16 snapshot (615,656 airframes): tolerates shrinkage, rejects truncation.
ADSBX_DB_MIN_ROWS = 500_000


@dag(
    dag_id="ingest_adsbx_db",
    description="Weekly ADSBx basic-ac-db JSONL -> bronze.adsbx_aircraft_db (registry type fill)",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    # 02:30 JST Monday — right after ingest_aircraft_db (02:00) so the same transform window sees both.
    schedule="30 17 * * 0",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "bronze", "adsbx"],
)
def ingest_adsbx_db():

    @task
    def download_and_land(**context) -> dict:
        import gzip
        import io

        import httpx
        import polars as pl

        from include import manifest
        from include.s3_helpers import write_parquet

        buf = io.BytesIO()
        with httpx.stream("GET", ADSBX_DB_URL, timeout=300.0, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                buf.write(chunk)
        # Full-file inference: the default 100-row window can type a sparse field (year, icaotype) from
        # nulls only and then raise mid-parse when values appear later in the 615k-row file.
        df = pl.read_ndjson(io.BytesIO(gzip.decompress(buf.getvalue())), infer_schema_length=None)

        # Registry-grade 6-hex keys only — TIS-B/garbage addresses can never join anything.
        df = df.filter(pl.col("icao").str.contains(r"^[0-9a-fA-F]{6}$"))

        if df.height < ADSBX_DB_MIN_ROWS:
            raise ValueError(f"basic-ac-db snapshot suspiciously small ({df.height} rows) — refusing to land")

        # Manual runs carry no logical_date in Airflow 3; run_after is always set.
        logical = context.get("logical_date") or context["dag_run"].run_after
        df = df.select(
            pl.col("icao").str.to_lowercase().alias("icao24"),
            pl.col("reg").alias("registration"),
            pl.col("icaotype"),
            pl.col("short_type"),
            pl.col("year").cast(pl.UInt16, strict=False),
            pl.col("manufacturer"),
            pl.col("model"),
            pl.col("ownop"),
            pl.col("faa_pia").cast(pl.UInt8),
            pl.col("faa_ladd").cast(pl.UInt8),
            pl.col("mil").cast(pl.UInt8),
            pl.lit(logical.strftime("%Y-%m-%d")).alias("as_of_date"),
            pl.lit(logical.isoformat()).alias("ingested_at"),
        )

        key = f"bronze/adsbx_db_raw/dt={logical.strftime('%Y-%m-%d')}/adsbx_db.parquet"
        uri = write_parquet(df, key)
        manifest.record_load(object_uri=uri, snapshot_min=None, snapshot_max=None, row_count=df.height)
        return {"rows": df.height, "uri": uri, "as_of": logical.strftime("%Y-%m-%d")}

    @task(outlets=[bronze_adsbx_db_table])
    def load_to_clickhouse() -> dict:
        # Raise on failure so a broken reload reds the weekly run rather than silently serving a stale DB.
        from include.clickhouse import backfill_adsbx_db

        result = backfill_adsbx_db()
        if not result.get("ok"):
            raise RuntimeError(f"CH adsbx_aircraft_db load failed: {result}")
        # download_and_land just wrote a file, so an empty-glob "skipped" here means the load path is broken.
        if result.get("skipped"):
            raise RuntimeError(f"CH adsbx_aircraft_db reload saw an empty glob after a fresh land: {result}")
        return result

    download_and_land() >> load_to_clickhouse()


ingest_adsbx_db()
