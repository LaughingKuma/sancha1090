from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag
from airflow.providers.standard.operators.bash import BashOperator

from include.adsb_assets import adsb_bronze_table


_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


@dag(
    dag_id="transform_adsb_silver",
    description="Build silver ADS-B dims + fct (dbt-clickhouse) from the ClickHouse bronze.adsb_states table",
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

    # +tag:adsb pulls in dim_aircraft_registry (the one cross-lane ancestor dim_aircraft depends on);
    # tag:ch_mv = the P4 ADS-B aggregates served by self-maintaining MVs, excluded from the rebuild + tests.
    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd="run --select +tag:adsb --exclude tag:ch_mv"),
    )
    # dbt test (same selection) is the all_success leaf — a run or data-quality failure reds the run.
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd="test --select +tag:adsb --exclude tag:ch_mv"),
    )

    dbt_run_ch >> dbt_test_ch


transform_adsb_silver()
