from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="ch_serving_parity",
    description="Serving gate: red + alert if CH bronze falls short of the source Parquet or a served mart goes stale",
    start_date=pendulum.datetime(2026, 6, 21, tz="UTC"),
    schedule="*/15 * * * *",
    catchup=False,
    max_active_runs=1,
    # Active on a clean deploy without a manual unpause (compose defaults DAGS_ARE_PAUSED_AT_CREATION=true);
    # this is a protection gate, so it must run from first boot.
    is_paused_upon_creation=False,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=3),
    },
    tags=["sancha1090", "clickhouse", "parity"],
)
def ch_serving_parity():

    @task
    def gate() -> dict:
        # P6.5: validate CH against the SOURCE Parquet (ground truth) + wall-clock freshness — no Trino. The
        # 2026-06-21 diagnostic proved Iceberg/Trino drifted from the Parquet while CH matches it byte-exact, so
        # CH-vs-Trino was gating on a broken oracle. Reds the run if CH bronze is short of source or a mart stalls.
        from include.ch_parity import run_source_gate

        return run_source_gate()

    @task
    def value_gate() -> dict:
        # Own task so a transform/MV value defect reds distinctly from completeness/freshness and the postgres
        # watermark dependency stays isolated.
        from include.ch_served_value import run_value_gate

        return run_value_gate()

    @task
    def path_coverage() -> dict:
        # Trajectory-coverage alarm (rung 1): red if fct_flight_path stalls or the adsblol
        # share cliffs. Parallel to value_gate so its red never stops the served-value watermark.
        from include.ch_parity import run_path_coverage_gate

        return run_path_coverage_gate()

    # gate >> value_gate: its oracle is bronze, so it must only advance the watermark after completeness passes —
    # else a meaningless pass on incomplete bronze ages the discrepancy out of the recheck window.
    g = gate()
    g >> value_gate()
    g >> path_coverage()


ch_serving_parity()
