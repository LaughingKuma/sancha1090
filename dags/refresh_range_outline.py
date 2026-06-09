from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="refresh_range_outline",
    description="Recompute the receiver range outline from bronze history → RisingWave (livemap)",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    schedule="40 5 * * *",  # daily; the coverage envelope only grows slowly, off the maintenance slots
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "risingwave", "adsb", "livemap", "v4"],
)
def refresh_range_outline():

    @task
    def refresh() -> int:
        # shared with the manual/first-boot path — one code path computes from bronze + loads RW
        from include.range_outline import refresh_range_outline as run

        return run()

    refresh()


refresh_range_outline()
