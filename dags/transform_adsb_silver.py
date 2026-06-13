from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag
from airflow.providers.standard.operators.bash import BashOperator

from include.adsb_assets import adsb_bronze_table


# Scoped to tag:adsb so it never races transform_marts' REPLACE of the states models (see iceberg-marts-optimize-race).
_DBT = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target trino --no-use-colors"


@dag(
    dag_id="transform_adsb_silver",
    description="Build silver ADS-B dims + fct (dbt-trino) from the Iceberg bronze.adsb_states table",
    schedule=[adsb_bronze_table],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "silver", "adsb"],
)
def transform_adsb_silver():

    dbt_seed = BashOperator(
        task_id="dbt_seed",
        bash_command=_DBT.format(cmd="seed --select tag:adsb"),
    )
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=_DBT.format(cmd="run --select tag:adsb"),
    )
    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=_DBT.format(cmd="test --select tag:adsb"),
    )

    dbt_seed >> dbt_run >> dbt_test


transform_adsb_silver()
