from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="maintain_bronze_dedup",
    description="Daily OPTIMIZE FINAL on the ReplacingMergeTree bronze tables (opensky_states + adsb_states + swim_flightdata) so a replay surplus can't accumulate un-merged",
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
        # The content-fp gate reads logical truth, so it can't see an un-merged physical replay surplus — force the
        # daily merge on the RMT tables (each self-skips a not-yet-RMT/absent table, so no churn pre-migration). Raises.
        from include.clickhouse import optimize_states_final

        return {t: optimize_states_final(t) for t in ("opensky_states", "adsb_states", "swim_flightdata")}

    optimize()


maintain_bronze_dedup()
