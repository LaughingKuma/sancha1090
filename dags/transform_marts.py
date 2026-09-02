from __future__ import annotations

from airflow.sdk import dag, task

from include.dag_dbt import dbt_run_test
from include.dag_defaults import default_args


@dag(
    dag_id="transform_marts",
    description="Build dbt-clickhouse silver + gold marts from the ClickHouse bronze tables",
    # Cron floor: the marts rebuild full history per run and no SLA needs 4-min freshness (ch_parity.py:
    # 2h/3d; live is RisingWave); :02 avoids :00 where the */15 parity gate clusters.
    # Drop to a 5-min floor only if a re-measure with scripts/ch_transform_cost.sh shows a tick under
    # ~540 CPU-s and under 90 s wall -- above it the 5-min floor breaks the <30% duty target.
    schedule="2-59/10 * * * *",
    catchup=False,
    # Prevents concurrent execution, not queuing -- a sustained >10-min run still builds a queue of scheduled runs.
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "silver", "gold"],
)
def transform_marts():

    # tag:adsb/flights are built by their own lanes; the P4 aggregates are served by self-maintaining MVs
    # (include/ch_incremental_mvs.py, applied by ensure_ch_mvs below), not dbt models.
    dbt_run_ch, dbt_test_ch = dbt_run_test("--exclude tag:adsb tag:flights")

    @task(task_id="ensure_ch_mvs", trigger_rule="all_done")
    def ensure_ch_mvs() -> dict:
        # Self-heals the _acc MVs + serving views so a fresh deploy needs no manual init. all_done because a
        # broken MV object is often WHY dbt_run_ch red; the run still reds on dbt_run_ch either way.
        from include.ch_incremental_mvs import ensure

        return ensure()

    @task(task_id="push_flight_routes")
    def push_flight_routes() -> int:
        # CH -> RisingWave route-memory publish, gated on a test-passing reconciled build; the 7-day route
        # lookback (include/flight_routes.py) makes the 10-min cadence immaterial -- it just rides this DAG.
        from include.flight_routes import refresh_flight_routes

        return refresh_flight_routes()

    # push_flight_routes is an all_success leaf, so a run/test failure propagates and reds the run; ensure_ch_mvs
    # runs all_done (heal-always) and can't mask that — dbt_run_ch's own failure is the red.
    dbt_run_ch >> ensure_ch_mvs()
    dbt_test_ch >> push_flight_routes()


transform_marts()
