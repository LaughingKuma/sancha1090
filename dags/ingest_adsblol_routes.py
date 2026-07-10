from __future__ import annotations

import logging
from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

log = logging.getLogger("ingest_adsblol_routes")


@dag(
    dag_id="ingest_adsblol_routes",
    description="Resolve overflight route backstory from adsb.lol global traces",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    # 12:00 JST: adsb.lol has published yesterday's globe_history by early UTC morning,
    # and transform_marts has already rebuilt fct_flights_reconciled for D-1's legs.
    schedule="0 3 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["sancha1090", "bronze", "adsblol", "v6"],
)
def ingest_adsblol_routes():

    @task
    def fetch_and_land(**context) -> dict:
        from include.adsblol_routes import run_daily

        end_dt = context.get("data_interval_end") or context["dag_run"].run_after
        return run_daily((end_dt - timedelta(days=1)).date())

    @task
    def load_to_clickhouse(_res: dict) -> dict:
        # Segments are the lane's product, so their failure reds the run; paths (a detection aid for
        # backfill_adsblol_resegment.py) self-heal via pending state, so a paths-only failure only warns.
        from include.clickhouse import (
            load_adsblol_paths_pending_to_ch,
            load_adsblol_segments_pending_to_ch,
        )

        segs = load_adsblol_segments_pending_to_ch()
        paths = load_adsblol_paths_pending_to_ch()
        if not segs.get("ok"):
            raise RuntimeError(f"CH adsblol segments load failed: segments={segs} paths={paths}")
        if not paths.get("ok"):
            log.warning("CH adsblol paths load failed (self-heals via pending state): %s", paths)
        return {"segments": segs, "paths": paths}

    load_to_clickhouse(fetch_and_land())


ingest_adsblol_routes()
