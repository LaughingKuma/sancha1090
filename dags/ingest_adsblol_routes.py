from __future__ import annotations

from datetime import date, timedelta

import pendulum

from airflow.sdk import dag, task
from airflow.sdk.bases.sensor import PokeReturnValue

from include.dag_defaults import default_args

def _window(context) -> tuple[list[date], bool, tuple[str, ...], int]:
    from include.adsblol_release import KIND_SUFFIXES, MIN_TRACES, sweep_days

    conf = (context["dag_run"].conf or {}) if context.get("dag_run") else {}
    end_dt = context.get("data_interval_end") or context["dag_run"].run_after
    day = (end_dt - timedelta(days=1)).date()
    days = (sorted(date.fromisoformat(d) for d in conf["trace_days"])
            if conf.get("trace_days") else sweep_days(day))
    kind = conf.get("kind")
    if kind is not None and kind not in KIND_SUFFIXES:
        raise ValueError(f"kind must be one of {KIND_SUFFIXES} (got {kind!r})")
    min_traces = int(conf["min_traces"]) if conf.get("min_traces") is not None else MIN_TRACES
    return days, bool(conf.get("force")), (kind,) if kind else KIND_SUFFIXES, min_traces


@dag(
    dag_id="ingest_adsblol_routes",
    description="Land overflight route traces from the daily adsb.lol GitHub release",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    # Day D's prod-0 release publishes ~03:22-03:28Z on D+1 and staging-0 anywhere 04:30-20:40Z
    # (measured 08-08..08-20), so the sensor probes for it rather than the schedule assuming it.
    schedule="0 4 * * *",
    catchup=False,
    max_active_runs=1,
    default_args=default_args(retries=2, delay_min=5),
    tags=["sancha1090", "bronze", "adsblol", "v6"],
)
def ingest_adsblol_routes():

    # Reschedule frees the worker slot between pokes; 17 h covers the latest staging-0 publish
    # seen (20:40Z) and still ends before the next 04:00Z tick.
    @task.sensor(poke_interval=1800, timeout=17 * 3600, mode="reschedule")
    def wait_for_release(**context) -> PokeReturnValue:
        import logging

        from include.adsblol_release import ProbeFailed, find_release

        days, _force, kinds, _min_traces = _window(context)
        try:
            ref = find_release(max(days), kinds)
        except ProbeFailed as exc:  # a GitHub hiccup is not-yet-published, not a failed poke
            logging.getLogger(__name__).warning("release probe failed: %s", exc)
            return PokeReturnValue(is_done=False, xcom_value=None)
        return PokeReturnValue(is_done=ref is not None, xcom_value=ref.tag if ref else None)

    @task(trigger_rule="all_done")
    def land_releases(**context) -> dict:
        import logging

        from include.adsblol_release import land_day_if_due

        log = logging.getLogger(__name__)
        days, force, kinds, min_traces = _window(context)
        newest = max(days)
        results: dict = {}
        failed: list[str] = []
        for d in days:
            try:
                res = land_day_if_due(d, force=force, kinds=kinds, min_traces=min_traces)
            except Exception as exc:  # one day's failure must not starve the other sweep days
                log.warning("release landing failed for %s", d, exc_info=True)
                failed.append(f"{d} ({exc})")
                continue
            results[d.isoformat()] = res
            if res.get("status") == "unpublished" and d != newest:
                # The sensor already reds the newest day; an older one gets 3 more ticks.
                log.warning("no adsb.lol release published yet for %s", d)
        # A day's marker stops the next sweep re-streaming it, so a per-trace error is only ever
        # seen on the tick that landed it: red that run, never the marker re-reads that follow.
        failed += [f"{d} ({res['errors']} error pair(s))" for d, res in sorted(results.items())
                   if res.get("status") == "landed" and res.get("errors")]
        if failed:
            raise RuntimeError(f"release landing failed for day(s): {'; '.join(failed)}")
        return results

    @task(trigger_rule="all_done")
    def load_to_clickhouse(_land_res: dict | None) -> dict:
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
        if _land_res is None:
            raise RuntimeError("adsb.lol release landing failed; successful days were loaded")
        return {"segments": segs, "paths": paths}

    waited = wait_for_release()
    landed = land_releases()
    waited >> landed
    load_to_clickhouse(landed)


ingest_adsblol_routes()
