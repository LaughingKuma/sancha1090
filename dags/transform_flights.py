from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_flights_table

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


@dag(
    dag_id="transform_flights",
    description="Build tag:flights dbt-clickhouse models from bronze flights/registry",
    schedule=[bronze_flights_table],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "silver", "gold", "v5"],
)
def transform_flights():

    # The lane refs its cross-lane seed deps (registry -> dim_hex_country; fact_flights -> dim_airports;
    # agg_operator_traffic -> dim_airlines), seeded once by the clickhouse-marts-init service / ch_setup_marts.sh.
    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd="run --select tag:flights"),
    )
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd="test --select tag:flights"),
    )

    # Linear gate: build -> test. The RW route publish moved to transform_marts (SP2: route source is now
    # gold_ch.fct_flights_reconciled, built there).
    dbt_run_ch >> dbt_test_ch


transform_flights()
