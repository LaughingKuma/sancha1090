from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

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
    def cohort_fetch_and_land(**context) -> dict:
        from include.adsblol_routes import rooftop_cohort, run_daily

        end_dt = context.get("data_interval_end") or context["dag_run"].run_after
        day = (end_dt - timedelta(days=1)).date()
        return run_daily(
            day,
            targets=rooftop_cohort(day),
            workers=2,
            include_error_retries=True,
            include_missing_retries=True,
            raise_on_errors=True,
        )

    @task(trigger_rule="all_done")
    def fetch_and_land(**context) -> dict:
        from include.adsblol_routes import run_daily

        end_dt = context.get("data_interval_end") or context["dag_run"].run_after
        day = (end_dt - timedelta(days=1)).date()
        # D-3..D sweep: the flights lane's D-2 arrival pull lands 14:30 UTC, after this DAG's
        # 03:00 tick, so late arrivals need a fourth shot; the ledger dedups re-proposals.
        results: dict = {}
        failed: list[str] = []
        for d in (day - timedelta(days=n) for n in (3, 2, 1, 0)):
            try:
                results[d.isoformat()] = run_daily(d, raise_on_errors=True)
            except Exception as exc:  # one day's failure must not starve the other sweep days
                failed.append(f"{d} ({exc})")
        if failed:
            raise RuntimeError(f"route sweep day(s) failed: {'; '.join(failed)}")
        return results

    @task(trigger_rule="all_done")
    def load_to_clickhouse(_cohort_res: dict | None, _route_res: dict | None) -> dict:
        # Attempt both pending lanes before raising so either can progress, but both are products now:
        # any failure must keep the DAG red while the pending manifest makes the retry idempotent.
        from include.clickhouse import (
            load_adsblol_paths_pending_to_ch,
            load_adsblol_segments_pending_to_ch,
        )

        segs = load_adsblol_segments_pending_to_ch()
        paths = load_adsblol_paths_pending_to_ch()
        if not segs.get("ok"):
            raise RuntimeError(f"CH adsblol segments load failed: segments={segs} paths={paths}")
        if not paths.get("ok"):
            raise RuntimeError(f"CH adsblol paths load failed: segments={segs} paths={paths}")
        if _cohort_res is None or _route_res is None:
            raise RuntimeError("one or more adsb.lol fetch lanes failed; successful pairs were loaded")
        return {"segments": segs, "paths": paths}

    # The two fetch TASKS run one at a time (never overlapping) so their worker pools never stack;
    # each task bounds its own concurrency internally. The ledger dedups the cohort/route overlap.
    cohort = cohort_fetch_and_land()
    fetched = fetch_and_land()
    cohort >> fetched
    load_to_clickhouse(cohort, fetched)


ingest_adsblol_routes()
