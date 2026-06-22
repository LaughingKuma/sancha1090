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

    gate()


ch_serving_parity()
