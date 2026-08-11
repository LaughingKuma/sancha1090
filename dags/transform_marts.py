from __future__ import annotations

from airflow.sdk import dag, task

from include.assets import bronze_states_table
from include.dag_dbt import dbt_run_test
from include.dag_defaults import default_args


@dag(
    dag_id="transform_marts",
    description="Build dbt-clickhouse silver + gold marts from the ClickHouse bronze tables",
    schedule=[bronze_states_table],
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "silver", "gold"],
)
def transform_marts():

    # tag:adsb/flights are built by their own lanes; the P4 aggregates are served by self-maintaining MVs
    # (include/ch_incremental_mvs.py, applied by ensure_ch_mvs below), not dbt models.
    dbt_run_ch, dbt_test_ch = dbt_run_test("--exclude tag:adsb tag:flights")

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
    dbt_run_ch >> ensure_ch_mvs()
    dbt_test_ch >> push_flight_routes()


transform_marts()
