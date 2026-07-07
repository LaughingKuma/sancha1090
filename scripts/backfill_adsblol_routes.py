from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where Garage/manifest endpoints resolve; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

import polars as pl

from include import adsblol_backfill as ab
from include import adsblol_routes as routes
from include import manifest
from include.db import analytics_engine
from include.s3_helpers import get_bucket, write_parquet
from scripts.backfill_adsblol_states import (
    _day_range,
    _manifest_status,
    _open_release,
    _quality_gate,
)


def _target_hexes() -> set[str]:
    from include.clickhouse import ch_client
    import os

    gold = os.environ.get("CH_GOLD_SCHEMA", "gold_ch")
    client = ch_client()
    try:
        rows = client.query(
            f"SELECT DISTINCT lower(icao24) FROM {gold}.fct_flights_reconciled "
            f"WHERE (origin_icao IS NULL OR dest_icao IS NULL) AND icao24 IS NOT NULL"
        ).result_rows
    finally:
        client.close()
    return {r[0] for r in rows if r[0]}


def _day_frames(day: date, reader: Any, targets: set[str],
                min_traces: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    rows: list[dict] = []
    path_rows: list[dict] = []
    members = corrupt = 0
    for name, data in ab.iter_trace_members(reader):
        members += 1
        if data is None:
            corrupt += 1
            continue
        hexid = ab.member_icao(name)
        if hexid is None or hexid not in targets:
            continue
        try:
            doc = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            corrupt += 1
            continue
        segs = routes.trace_segments(doc, day)
        rows.extend(segs)
        path_rows.extend(routes.trace_paths(doc, day, segs))
        if members % 50_000 == 0:
            print(f"{day}: {members} traces scanned, {len(rows)} segments kept")
    _quality_gate(members, corrupt, min_traces)
    print(f"{day}: done — {members} traces ({corrupt} corrupt), "
          f"{len(rows)} segments, {len(path_rows)} path points")
    return routes.segments_frame(rows), routes.paths_frame(path_rows)


def run(start: date, end: date, min_traces: int, dry_run: bool) -> int:
    bucket = get_bucket()
    engine = analytics_engine()
    targets = _target_hexes()
    print(f"{len(targets)} target hexes")
    failures: list[str] = []
    for day in _day_range(start, end):
        # Fixed part-backfill name (vs the daily lane's per-run stamps): it doubles as the day's resume marker.
        key = f"bronze/adsblol_flight_segments/dt={day.isoformat()}/part-backfill.parquet"
        uri = f"s3://{bucket}/{key}"
        if _manifest_status(uri, engine=engine) != "missing":
            print(f"{day}: already recorded — skipping")
            continue
        try:
            reader = _open_release(day)
            if reader is None:
                failures.append(f"{day} (no release found)")
                print(f"{day}: no release found")
                continue
            df, pdf = _day_frames(day, reader, targets, min_traces)
            if dry_run:
                print(f"{day}: DRY RUN — {df.height} segments, {pdf.height} path points, would write {uri}")
                continue
            # Paths land BEFORE the segments manifest record: the segments URI is the day's
            # resume marker, so a crash/failure between the two writes re-runs the whole day.
            if pdf.height:
                pkey = f"bronze/adsblol_flight_paths/dt={day.isoformat()}/part-backfill.parquet"
                puri = write_parquet(pdf, pkey)
                ts = pdf.get_column("ts")
                manifest.record_load(puri, int(ts.min()), int(ts.max()), pdf.height, engine=engine)
            write_parquet(df, key)
            starts = df.get_column("seg_start")
            manifest.record_load(
                uri,
                int(starts.min()) if df.height else None,
                int(starts.max()) if df.height else None,
                df.height,
                engine=engine,
            )
        except Exception as exc:  # noqa: BLE001 — one bad day must not abort the wave; reruns retry it
            failures.append(f"{day} ({exc})")
            print(f"{day}: FAILED — {exc}")
            continue
    if failures:
        print(f"{len(failures)} day(s) failed:")
        for f in failures:
            print(f"  {f}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill bronze.adsblol_flight_segments from adsb.lol globe_history")
    p.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    p.add_argument("--end", required=True, help="last day inclusive, YYYY-MM-DD")
    p.add_argument("--min-traces", type=int, default=10_000,
                   help="fail a day below this many trace files (default 10000)")
    p.add_argument("--dry-run", action="store_true", help="stream + extract only, write nothing")
    args = p.parse_args()
    return run(date.fromisoformat(args.start), date.fromisoformat(args.end),
               args.min_traces, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
