from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_swim_table

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


@dag(
    dag_id="transform_swim",
    description="Build tag:swim dbt-clickhouse models from bronze SWIM TFMData",
    schedule=[bronze_swim_table],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "silver", "swim"],
)
def transform_swim():

    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd="run --select tag:swim"),
    )
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd="test --select tag:swim"),
    )

    # Linear gate: build -> test.
    dbt_run_ch >> dbt_test_ch


transform_swim()
