from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.assets import bronze_swim_table


@dag(
    dag_id="tableize_swim",
    description="Load newly-landed SWIM parquet into ClickHouse bronze.swim_flightdata",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    schedule="*/5 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "clickhouse", "swim"],
)
def tableize_swim():

    @task(outlets=[bronze_swim_table])
    def load_pending_to_clickhouse() -> dict:
        # Check ok before files: a genuine failure can also report files=0, which must RED not SKIP.
        # Cron-driven (producer is an always-on consumer service, not an asset-emitting DAG); most ticks land
        # nothing, so skip rather than emit the asset and trigger a no-op transform_swim rebuild.
        from airflow.exceptions import AirflowSkipException

        from include.clickhouse import load_swim_pending_to_ch

        result = load_swim_pending_to_ch()
        if not result.get("ok"):
            raise RuntimeError(f"CH swim bronze load failed: {result}")
        if result.get("files", 0) == 0:
            raise AirflowSkipException("no pending swim files to load")
        return result

    load_pending_to_clickhouse()


tableize_swim()
