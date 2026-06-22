from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="maintain_bronze_dedup",
    description="Daily OPTIMIZE FINAL on bronze.opensky_states so the ReplacingMergeTree replay surplus can't accumulate un-merged",
    start_date=pendulum.datetime(2026, 6, 22, tz="UTC"),
    schedule="30 18 * * *",  # ~03:30 JST, off-peak (Airflow schedules are UTC)
    catchup=False,
    max_active_runs=1,
    # A protection task, like ch_serving_parity — runs from first boot without a manual unpause.
    is_paused_upon_creation=False,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "clickhouse", "maintenance"],
)
def maintain_bronze_dedup():

    @task
    def optimize() -> dict:
        # The content-fp serving gate reads logical truth (distinct content), so it can't see a physical replay
        # surplus that CH hasn't merged yet — force the merge daily to GUARANTEE bounded growth. Raises on failure.
        from include.clickhouse import optimize_states_final

        return optimize_states_final()

    optimize()


maintain_bronze_dedup()
