from __future__ import annotations

import pendulum

from airflow.sdk import dag, task

from include.dag_defaults import default_args


@dag(
    dag_id="ingest_vrs_routes",
    description="Daily vradarserver standing-data route pull (adsb.lol mirror) -> dim.dim_vrs_routes (SP4)",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    # 02:00 JST daily: the mirror refreshes hourly but churn is slow; off-peak, clear of the dim refreshes.
    schedule="0 17 * * *",
    catchup=False,
    max_active_runs=1,
    default_args=default_args(retries=2, delay_min=15),
    tags=["sancha1090", "dim", "reconcile"],
)
def ingest_vrs_routes():

    @task
    def load_vrs_routes() -> dict:
        # Fail-loud lane: header drift, a short fetch, or a CH failure must red the run (no silent stale dim).
        from include.vrs_routes import load_vrs_routes_to_ch

        return load_vrs_routes_to_ch()

    load_vrs_routes()


ingest_vrs_routes()
