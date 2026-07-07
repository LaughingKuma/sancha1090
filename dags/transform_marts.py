from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_states_table

_DBT_CH = "cd /opt/airflow/dbt/sancha1090 && dbt {cmd} --profiles-dir . --target clickhouse --no-use-colors"


@dag(
    dag_id="transform_marts",
    description="Build dbt-clickhouse silver + gold marts from the ClickHouse bronze tables",
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

    # tag:adsb/flights are built by their own lanes; tag:ch_mv = the P4 aggregates served by self-maintaining
    # MVs (include/ch_incremental_mvs.py), excluded from the scheduled rebuild + its tests.
    dbt_run_ch = BashOperator(
        task_id="dbt_run_ch",
        bash_command=_DBT_CH.format(cmd="run --exclude tag:adsb tag:flights tag:ch_mv"),
    )
    # dbt test (same selection as the run) gates the canonical build — a data-quality failure reds the run.
    dbt_test_ch = BashOperator(
        task_id="dbt_test_ch",
        bash_command=_DBT_CH.format(cmd="test --exclude tag:adsb tag:flights tag:ch_mv"),
    )

    @task(task_id="ensure_ch_mvs")
    def ensure_ch_mvs() -> dict:
        # Self-heal so a fresh deploy doesn't need the manual init DAG before Superset reads CH: idempotently
        # (re)create the serving views + _acc MVs. Default all_success so a dbt_run_ch failure reds the run
        # (an all_done leaf here would mask it, since ensure() is best-effort and never raises).
        from include.ch_incremental_mvs import ensure

        return ensure()

    @task(task_id="push_flight_routes")
    def push_flight_routes() -> int:
        # CH -> RisingWave route-memory publish, gated on a test-passing reconciled build; runs on the frequent
        # bronze_states_table tick (SP2 moved it here from transform_flights) so routes stay fresh within minutes.
        from include.flight_routes import refresh_flight_routes

        return refresh_flight_routes()

    # Two all_success leaves (push_flight_routes, ensure_ch_mvs): a run/test failure propagates and reds the run.
    dbt_run_ch >> dbt_test_ch
    dbt_run_ch >> ensure_ch_mvs()
    dbt_test_ch >> push_flight_routes()


transform_marts()
