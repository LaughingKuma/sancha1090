from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_states_table


WATERMARK_NAME = "transform_marts"
SEED_OFFSET = timedelta(days=7)
RETENTION = timedelta(days=30)


@dag(
    dag_id="transform_marts",
    description="Append Iceberg deltas to staging.raw_states, trim retention, build dbt models",
    schedule=[bronze_states_table],
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
        import os
        from datetime import datetime, timezone

        import polars as pl
        import sqlalchemy as sa
        from pyiceberg.expressions import GreaterThan

        from include import iceberg as ib
        from include import watermark

        watermark.ensure_table()
        wm = watermark.get_or_seed(WATERMARK_NAME, SEED_OFFSET)
        print(f"watermark: {wm.isoformat()}")

        catalog = ib.get_catalog()
        table = catalog.load_table(ib.QUALIFIED)
        # committed_at (Iceberg-commit time) anchors the watermark; backfilled rows
        # whose snapshot_time is old still get picked up if their commit is recent.
        arrow_table = table.scan(row_filter=GreaterThan("committed_at", wm)).to_arrow()

        if arrow_table.num_rows == 0:
            print("no new rows above watermark; skipping load")
            return 0

        df = pl.from_arrow(arrow_table)
        new_max = df["committed_at"].max()

        # Existing stg_states expects epoch ints + iso strings (legacy schema).
        # committed_at is an Iceberg-only column, not present in staging.raw_states.
        df = df.with_columns(
            pl.col("snapshot_time").dt.epoch("s").alias("snapshot_time"),
            pl.col("time_position").dt.epoch("s").alias("time_position"),
            pl.col("last_contact").dt.epoch("s").alias("last_contact"),
            pl.col("ingested_at").dt.strftime("%Y-%m-%dT%H:%M:%S%z").alias("ingested_at"),
        ).drop("committed_at")

        url = (
            f"postgresql+psycopg2://"
            f"{os.environ['ANALYTICS_PG_USER']}:{os.environ['ANALYTICS_PG_PASSWORD']}"
            f"@{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}"
            f"/{os.environ['ANALYTICS_PG_DB']}"
        )
        engine = sa.create_engine(url)
        pdf = df.to_pandas()

        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA IF NOT EXISTS staging"))
            pdf.to_sql(
                "raw_states",
                conn,
                schema="staging",
                if_exists="append",
                index=False,
                chunksize=10000,
            )
            floor_epoch = int((datetime.now(timezone.utc) - RETENTION).timestamp())
            deleted = conn.execute(
                sa.text("DELETE FROM staging.raw_states WHERE snapshot_time < :floor"),
                {"floor": floor_epoch},
            )
            watermark.advance(WATERMARK_NAME, new_max, conn)

        print(
            f"appended {len(pdf)} rows, deleted {deleted.rowcount} below retention, "
            f"watermark advanced to {new_max.isoformat()}"
        )
        return len(pdf)

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

    load_count = load_states_to_pg()
    load_count >> dbt_deps >> dbt_run >> dbt_test


transform_marts()
