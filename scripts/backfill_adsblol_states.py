from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Iterator, Optional

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where Garage/manifest endpoints resolve; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

import polars as pl
import sqlalchemy as sa

from include import adsblol_backfill as ab
from include import manifest
from include.db import analytics_engine
from include.s3_helpers import get_bucket, write_parquet

USER_AGENT = "sancha1090-backfill"
CORRUPT_RATIO_CEILING = 0.01

RAW_STATES_SCHEMA = {
    "icao24": pl.Utf8,
    "callsign": pl.Utf8,
    "origin_country": pl.Utf8,
    "time_position": pl.Int64,
    "last_contact": pl.Int64,
    "longitude": pl.Float64,
    "latitude": pl.Float64,
    "baro_altitude": pl.Float64,
    "on_ground": pl.Boolean,
    "velocity": pl.Float64,
    "true_track": pl.Float64,
    "vertical_rate": pl.Float64,
    "geo_altitude": pl.Float64,
    "squawk": pl.Utf8,
    "spi": pl.Boolean,
    "position_source": pl.Int32,
    "snapshot_time": pl.Int64,
    "region": pl.Utf8,
    "source": pl.Utf8,
}


def _quality_gate(members: int, corrupt: int, min_traces: int) -> None:
    # Tolerate isolated corrupt traces, but a desynced tar stream corrupts everything
    # after the bad spot — never commit a quietly-partial day. min_traces is a knob:
    # some upstream days are legitimately partial (e.g. 2026-05-05's 236 MB tar) and
    # need a deliberate lower floor to land.
    if members < min_traces or corrupt > members * CORRUPT_RATIO_CEILING:
        raise RuntimeError(f"day failed quality gate: {members} traces, {corrupt} corrupt")


def _day_range(start: date, end: date) -> Iterator[date]:
    # start<=end walks forward; start>end walks backward — lets a floor-hit during a
    # backward wave land against known-good days at the END of a run, not scattered
    # through the middle of one.
    step = timedelta(days=1) if start <= end else timedelta(days=-1)
    d = start
    while True:
        yield d
        if d == end:
            return
        d += step


class _MissingCounter:
    # Circuit breaker for a backward wave walking past adsb.lol's release floor
    # (~2023-03-10, not hardcoded/validated anywhere in this repo) — stop probing
    # nonexistent tags after N consecutive misses instead of grinding to --end.
    def __init__(self, stop_after: int):
        self._stop_after = stop_after
        self._streak = 0

    def record_missing(self) -> bool:
        self._streak += 1
        return self._streak >= self._stop_after

    def record_found(self) -> None:
        self._streak = 0


def _manifest_status(uri: str, engine: Optional[sa.Engine] = None) -> str:
    eng = engine or analytics_engine()
    stmt = sa.text(
        "SELECT ch_loaded_at IS NOT NULL AS done FROM ingestion_manifest WHERE object_uri = :uri"
    )
    with eng.begin() as conn:
        row = conn.execute(stmt, {"uri": uri}).first()
    if row is None:
        return "missing"
    return "ch_loaded" if row.done else "pending"


def _head_ok(url: str) -> bool:
    # Only a definitive 404 means "part doesn't exist" — treating a transient
    # 403/429/5xx as missing would silently truncate the tar part chain into a
    # partial (and committable) day.
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            last_exc = exc
        except urllib.error.URLError as exc:
            last_exc = exc
        time.sleep(2**attempt)
    raise RuntimeError(f"HEAD {url} kept failing: {last_exc}")


def _open_release(day: date) -> Optional[ab.ChainedReader]:
    for repo, tag in ab.release_candidates(day):
        # Sub-2GB days ship as one unsplit .tar; larger days split into aa/ab/...
        if _head_ok(ab.part_url(repo, tag)):
            parts = [""]
        else:
            parts = []
            for i in range(40):
                suffix = chr(ord("a") + i // 26) + chr(ord("a") + i % 26)
                if not _head_ok(ab.part_url(repo, tag, suffix)):
                    break
                parts.append(suffix)
        if not parts:
            continue
        print(f"{day}: using {repo}/{tag} ({len(parts)} part(s))")

        def opener(part: str, repo: str = repo, tag: str = tag):
            def _open():
                req = urllib.request.Request(
                    ab.part_url(repo, tag, part), headers={"User-Agent": USER_AGENT}
                )
                return urllib.request.urlopen(req, timeout=120)
            return _open

        return ab.ChainedReader([opener(p) for p in parts])
    return None


def _day_rows(day: date, reader: ab.ChainedReader, min_traces: int) -> pl.DataFrame:
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    rows: list[dict] = []
    members = 0
    corrupt = 0
    snapshot_count = 0
    for name, data in ab.iter_trace_members(reader):
        members += 1
        if data is None:
            corrupt += 1
            continue
        if ab.member_icao(name) is None:
            continue
        try:
            doc = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            corrupt += 1
            continue
        sampled = ab.resample_trace(doc, day_start)
        snapshot_count += len(sampled)
        rows.extend(sampled)
        # Full resolution over the whole Japan box; the 12-min snapshot tier above
        # exists only to keep the trend marts at live-lane cadence parity.
        rows.extend(ab.dense_rows(doc, day_start))
        if members % 50_000 == 0:
            print(f"{day}: {members} traces scanned, {len(rows)} rows kept")
    _quality_gate(members, corrupt, min_traces)
    print(
        f"{day}: done — {members} traces ({corrupt} corrupt, skipped), {len(rows)} rows "
        f"({snapshot_count} japan snapshots, {len(rows) - snapshot_count} full-res)"
    )
    df = pl.DataFrame(rows, schema=RAW_STATES_SCHEMA) if rows else pl.DataFrame(schema=RAW_STATES_SCHEMA)
    return df.with_columns(pl.lit(datetime.now(timezone.utc).isoformat()).alias("ingested_at"))


def run(start: date, end: date, min_traces: int, stop_after_missing: int, dry_run: bool) -> int:
    bucket = get_bucket()
    engine = analytics_engine()  # one pool for the whole run, not one per day
    missing = _MissingCounter(stop_after_missing)
    failures: list[str] = []
    for day in _day_range(start, end):
        key = f"bronze/adsblol_states_raw/dt={day.isoformat()}/source=adsblol/part-000.parquet"
        uri = f"s3://{bucket}/{key}"
        if _manifest_status(uri, engine=engine) != "missing":
            print(f"{day}: already recorded — skipping")
            missing.record_found()
            continue

        try:
            reader = _open_release(day)
        except Exception as exc:  # transient HEAD failures after retries — record and move on
            failures.append(f"{day} (release lookup failed: {exc})")
            print(f"{day}: FAILED — {exc}")
            continue

        if reader is None:
            floor_hit = missing.record_missing()
            print(f"{day}: no release found")
            if floor_hit:
                print(f"{stop_after_missing} consecutive missing days — floor reached, stopping")
                break
            continue
        missing.record_found()

        try:
            df = _day_rows(day, reader, min_traces)
        except Exception as exc:  # noqa: BLE001 — one bad day must not abort the wave; reruns retry it
            failures.append(f"{day} ({exc})")
            print(f"{day}: FAILED — {exc}")
            continue

        if dry_run:
            print(f"{day}: DRY RUN — {df.height} rows, would write to {uri}")
            continue

        write_parquet(df, key)
        epochs = df.get_column("snapshot_time")
        manifest.record_load(
            uri,
            int(epochs.min()) if df.height else None,
            int(epochs.max()) if df.height else None,
            df.height,
            engine=engine,
        )

    if failures:
        print(f"{len(failures)} day(s) failed:")
        for f in failures:
            print(f"  {f}")
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill bronze.adsblol_states from adsb.lol globe_history")
    p.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    p.add_argument("--end", required=True, help="last day inclusive, YYYY-MM-DD")
    p.add_argument("--min-traces", type=int, default=10_000,
                    help="fail a day below this many trace files (default 10000)")
    p.add_argument("--stop-after-missing", type=int, default=3,
                    help="stop after N consecutive days with no release found (default 3)")
    p.add_argument("--dry-run", action="store_true", help="fetch + quality-gate only, write nothing")
    args = p.parse_args()

    return run(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        args.min_traces,
        args.stop_after_missing,
        args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
