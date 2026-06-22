from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_flights_table

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


@dag(
    dag_id="transform_flights",
    description="Build tag:flights dbt-clickhouse models from bronze flights/registry, then push routes to RisingWave",
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

    @task
    def push_flight_routes() -> int:
        # CH -> RisingWave versioned publish: only after a fresh, test-passing gold_ch.fact_flights build.
        from include.flight_routes import refresh_flight_routes

        return refresh_flight_routes()

    # Linear gate: build -> test -> publish. A run or test failure reds the run and withholds the RW publish.
    dbt_run_ch >> dbt_test_ch >> push_flight_routes()


transform_flights()
