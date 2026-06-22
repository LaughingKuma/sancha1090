from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_states_table, raw_states_landed


@dag(
    dag_id="tableize_states",
    description="Load newly-landed raw state parquet into ClickHouse bronze.opensky_states",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[raw_states_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "clickhouse"],
)
def tableize_states():

    @task(outlets=[bronze_states_table])
    def load_pending_to_clickhouse() -> dict:
        # CH bronze is the canonical landing target; drains its own ch_loaded_at pending set. Raise on a load
        # failure so the task reds and the bronze asset (which triggers transform_marts) is NOT emitted on a
        # stale load — the per-batch loader already drains what it can before reporting ok=False.
        from include.clickhouse import load_states_pending_to_ch

        result = load_states_pending_to_ch()
        if not result.get("ok"):
            raise RuntimeError(f"CH states bronze load failed: {result}")
        return result

    load_pending_to_clickhouse()


tableize_states()
