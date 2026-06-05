from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="refresh_risingwave_dims",
    description="Reload RisingWave dim tables from the dbt seed CSVs (v4.1 live hot path)",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    schedule="15 5 * * 1",  # weekly; seeds are near-static, off the 03:30-04:35 maintenance slots
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["sancha1090", "risingwave", "adsb", "v4"],
)
def refresh_risingwave_dims():

    @task
    def reload() -> dict[str, int]:
        # shared with the risingwave-seed one-shot (first-boot load) — one code path
        from include.risingwave_dims import load_dims

        return load_dims()

    reload()


refresh_risingwave_dims()
