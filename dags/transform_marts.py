"""transform_marts: load bronze parquet to Postgres, build dbt models.

Stage 12: asset-triggered consumer that materializes the silver and gold
layers.

Design notes:
- TRUNCATE + INSERT for staging.raw_states. Simple, idempotent. At our
  data volume (~5k rows/snapshot × 144 snapshots/day = ~720k rows/day max)
  this is fine. At scale you'd switch to incremental loads partitioned
  by snapshot_time. 
- dbt called via BashOperator. Cosmos would give per-model task
  granularity but is overkill for 3 models.
- dbt test failures fail the DAG by design. Bad data should not silently
  land in marts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from airflow.sdk import dag, task, Asset
from airflow.providers.standard.operators.bash import BashOperator


bronze_states = Asset("s3://opensky/bronze/states/")


@dag(
    dag_id="transform_marts",
    description="Load bronze to Postgres, build dbt staging + marts",
    schedule=[bronze_states],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["opensky", "silver", "gold", "stage-12"],
)
def transform_marts():

    @task
    def load_states_to_pg() -> int:
        """Read all bronze states parquet, write to staging.raw_states.

        Strategy: read everything under bronze/states/, concat, replace
        the table. Idempotent for our volume; we lose history each run
        but the marts can be re-derived from whatever's currently bronze.
        """
        import os
        import polars as pl
        import sqlalchemy as sa
        from include.minio_helpers import get_s3fs, get_bucket

        fs = get_s3fs()
        bucket = get_bucket()
        prefix = f"{bucket}/bronze/states/"

        # Find every parquet file under the bronze/states/ prefix.
        try:
            files = fs.glob(f"{prefix}**/*.parquet")
        except FileNotFoundError:
            files = []

        if not files:
            print("No parquet files in bronze/states/. Skipping load.")
            return 0

        # Read and concat. diagonal_relaxed handles minor schema drift
        # (e.g. one file has an extra column another doesn't) by union-ing
        # columns and filling missing with nulls.
        frames = []
        for f in files:
            with fs.open(f, "rb") as fh:
                frames.append(pl.read_parquet(fh))

        df = pl.concat(frames, how="diagonal_relaxed")

        # Build SQLAlchemy connection from env-driven config.
        url = (
            f"postgresql+psycopg2://"
            f"{os.environ['ANALYTICS_PG_USER']}:{os.environ['ANALYTICS_PG_PASSWORD']}"
            f"@{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}"
            f"/{os.environ['ANALYTICS_PG_DB']}"
        )
        engine = sa.create_engine(url)

        # Ensure schema exists, then truncate the target if it already
        # exists. TRUNCATE (not DROP) keeps the table's identity intact,
        # so dbt views built on top of raw_states (e.g. stg_states) stay
        # valid across runs. if_exists='replace' would DROP+CREATE and
        # fail with DependentObjectsStillExist on the second run.
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS staging"))
            if sa.inspect(conn).has_table("raw_states", schema="staging"):
                conn.execute(sa.text("TRUNCATE TABLE staging.raw_states"))

        pdf = df.to_pandas()
        pdf.to_sql(
            "raw_states",
            engine,
            schema="staging",
            if_exists="append",
            index=False,
            chunksize=10000,
        )

        row_count = len(pdf)
        print(f"Loaded {row_count} rows into staging.raw_states")
        return row_count

    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt deps --profiles-dir . --no-use-colors"
        ),
    )

    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt run --profiles-dir . --no-use-colors"
        ),
    )

    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt test --profiles-dir . --no-use-colors"
        ),
    )

    # Task graph: load first, then dbt deps → run → test in sequence.
    load_count = load_states_to_pg()
    load_count >> dbt_deps >> dbt_run >> dbt_test


transform_marts()