from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_flights_table, raw_flights_landed


@dag(
    dag_id="tableize_flights",
    description="Load newly-landed raw flights parquet into ClickHouse bronze.opensky_flights",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    schedule=[raw_flights_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "clickhouse", "v5"],
)
def tableize_flights():

    @task(outlets=[bronze_flights_table])
    def load_pending_to_clickhouse() -> dict:
        # CH bronze is the canonical landing target; drains its own ch_loaded_at pending set. Raise on a load
        # failure so the task reds and the bronze asset (which triggers transform_flights) is NOT emitted stale.
        from include.clickhouse import load_flights_pending_to_ch

        result = load_flights_pending_to_ch()
        if not result.get("ok"):
            raise RuntimeError(f"CH flights bronze load failed: {result}")
        return result

    load_pending_to_clickhouse()


tableize_flights()
