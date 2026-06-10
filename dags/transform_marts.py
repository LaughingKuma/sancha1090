from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_states_table


@dag(
    dag_id="transform_marts",
    description="Build dbt-trino silver + gold marts from the Iceberg bronze table",
    schedule=[bronze_states_table],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "silver", "gold"],
)
def transform_marts():

    @task
    def ensure_bronze_tables() -> None:
        # Blank-warehouse safety: stg_states_history reads bronze.archive_states, which
        # only gets data from the one-shot v5.2 backfill — the source must exist (even
        # empty) for every 12-min tick regardless of whether a wave ever ran.
        from include import archive_iceberg

        archive_iceberg.ensure_archive_table()

    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt deps --profiles-dir . --no-use-colors"
        ),
    )

    # Builds fct_flight_legs, which refs tag:adsb relations (seeds + dim_aircraft + fct_adsb_state)
    # built by transform_adsb_silver — on a fresh deploy, run that DAG once before this one.
    dbt_run_trino = BashOperator(
        task_id="dbt_run_trino",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt run --profiles-dir . --target trino --no-use-colors --exclude tag:adsb tag:flights"
        ),
    )

    dbt_test_trino = BashOperator(
        task_id="dbt_test_trino",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt test --profiles-dir . --target trino --no-use-colors --exclude tag:adsb tag:flights"
        ),
    )

    ensure_bronze_tables() >> dbt_deps >> dbt_run_trino >> dbt_test_trino


transform_marts()
