from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="ch_incremental_mvs_init",
    description="One-time: create + seed the P4 self-maintaining ClickHouse AggregatingMergeTree MVs",
    start_date=pendulum.datetime(2026, 6, 20, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "clickhouse", "manual"],
)
def ch_incremental_mvs_init():

    @task
    def create_and_seed() -> dict:
        # Idempotent; conf {"reseed": true} forces a destructive truncate + re-seed.
        from airflow.sdk import get_current_context

        from include.ch_incremental_mvs import apply

        # Strict truthy parse: reseed TRUNCATEs, so a JSON-string "false" must NOT trigger it (bool("false")=True).
        raw = (get_current_context()["dag_run"].conf or {}).get("reseed", False)
        reseed = raw is True or str(raw).strip().lower() in ("1", "true", "yes", "on")
        return apply(reseed=reseed)

    create_and_seed()


ch_incremental_mvs_init()
