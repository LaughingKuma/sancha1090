from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="ingest_ladd",
    description="Weekly FAA LADD privacy-list pull (Garage dims/ladd_raw) -> dim.dim_ladd SCD2 (SP3b)",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    # 03:00 JST Tuesday, off-peak and clear of the Monday dim refreshes; the pull itself is a manual monthly upload.
    schedule="0 18 * * 1",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "dim", "ladd"],
)
def ingest_ladd():

    @task
    def load_ladd_pulls() -> dict:
        # Green no-op when the prefix is empty or every list is already seen; fails loud on a malformed pull.
        from include.ladd import load_ladd_pulls_to_ch

        return load_ladd_pulls_to_ch()

    @task
    def check_ladd_freshness() -> dict:
        # Compliance guardrail: SKIP until a first list ever lands, then FAIL if the newest pull ages past the SLA.
        from airflow.exceptions import AirflowSkipException

        from include.ladd import ladd_freshness_ch

        status, message = ladd_freshness_ch()
        if status == "skip":
            raise AirflowSkipException(message)
        if status == "fail":
            raise RuntimeError(message)
        return {"status": status, "message": message}

    load_ladd_pulls() >> check_ladd_freshness()


ingest_ladd()
