from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag
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
    tags=["opensky", "silver", "gold", "stage-12"],
)
def transform_marts():

    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt deps --profiles-dir . --no-use-colors"
        ),
    )

    dbt_run_trino = BashOperator(
        task_id="dbt_run_trino",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt run --profiles-dir . --target trino --no-use-colors"
        ),
    )

    dbt_test_trino = BashOperator(
        task_id="dbt_test_trino",
        bash_command=(
            "cd /opt/airflow/dbt/opensky && "
            "dbt test --profiles-dir . --target trino --no-use-colors"
        ),
    )

    dbt_deps >> dbt_run_trino >> dbt_test_trino


transform_marts()
