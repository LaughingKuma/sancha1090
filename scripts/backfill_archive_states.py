from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where Garage/Polaris/manifest endpoints resolve; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

import polars as pl
import sqlalchemy as sa

from include import archive_backfill as ab
from include import archive_iceberg
from include import manifest
from include.db import analytics_engine
from include.flights_iceberg import RAW_FLIGHTS_SCHEMA, flight_row
from include.s3_helpers import garage_pyarrow_fs, get_bucket, write_parquet

USER_AGENT = "sancha1090-backfill"

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

def _manifest_uris(like: str) -> dict[str, bool]:
    stmt = sa.text(
        "SELECT object_uri, iceberg_committed_at IS NOT NULL AS committed "
        "FROM public.ingestion_manifest WHERE object_uri LIKE :like"
    )
    with analytics_engine().begin() as conn:
        return {r.object_uri: r.committed for r in conn.execute(stmt, {"like": like})}


def _head_ok(url: str) -> bool:
    # Only a definitive 404 means "part doesn't exist" — treating a transient
    # 403/429/5xx as missing would silently truncate the tar part chain into a
    # partial (and committable) day.
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    last_exc: Exception | None = None
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
        time.sleep(2 ** attempt)
    raise RuntimeError(f"HEAD {url} kept failing: {last_exc}")


def _open_release(day: date) -> ab.ChainedReader | None:
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


def _day_rows(day: date, min_traces: int) -> pl.DataFrame | None:
    stream = _open_release(day)
    if stream is None:
        return None
    day_start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    rows: list[dict] = []
    members = 0
    corrupt = 0
    snapshot_count = 0
    for name, data in ab.iter_trace_members(stream):
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
    # Quality gate: tolerate isolated corrupt traces, but a desynced tar stream
    # corrupts everything after the bad spot — never commit a quietly-partial day.
    # min_traces is a knob: some upstream days are legitimately partial (e.g.
    # 2026-05-05's 236 MB tar) and need a deliberate lower floor to land.
    if members < min_traces or corrupt > members * 0.01:
        raise RuntimeError(f"day failed quality gate: {members} traces, {corrupt} corrupt")
    print(
        f"{day}: done — {members} traces ({corrupt} corrupt, skipped), {len(rows)} rows "
        f"({snapshot_count} japan snapshots, {len(rows) - snapshot_count} full-res)"
    )
    df = pl.DataFrame(rows, schema=RAW_STATES_SCHEMA) if rows else pl.DataFrame(schema=RAW_STATES_SCHEMA)
    return df.with_columns(pl.lit(datetime.now(timezone.utc).isoformat()).alias("ingested_at"))


def _already_appended(table, fingerprint: str) -> bool:
    # Scan recent snapshot summaries, not just the current one: a nightly OPTIMIZE
    # (or the wave's own) can commit between a crashed append and the rerun.
    recent = sorted(table.snapshots(), key=lambda s: s.timestamp_ms, reverse=True)[:50]
    return any(
        s.summary and s.summary.additional_properties.get("manifest_fingerprint") == fingerprint
        for s in recent
    )


def _append_day(table, uri: str, df: pl.DataFrame, allow_recovery: bool) -> None:
    # Content-aware (uri + row count) and gated on a pending manifest row: after a
    # rollback (rows deleted, manifest cleared) the old append's snapshot summary
    # still exists, and a bare per-URI fingerprint would "recover" the deleted day.
    fingerprint = hashlib.sha256(f"{uri}:{df.height}".encode()).hexdigest()
    if allow_recovery and _already_appended(table, fingerprint):
        manifest.mark_iceberg_committed([uri])
        print(f"recovered prior append for {uri}")
        return

    callsign_trim = pl.col("callsign").str.strip_chars()
    df = df.with_columns(
        pl.from_epoch(pl.col("snapshot_time"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("snapshot_time"),
        pl.from_epoch(pl.col("last_contact"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("last_contact"),
        pl.from_epoch(pl.col("time_position"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("time_position"),
        pl.col("ingested_at").str.to_datetime(time_zone="UTC").alias("ingested_at"),
        pl.when(callsign_trim == "").then(None).otherwise(callsign_trim).alias("callsign"),
        pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
    )
    df = df.select([f.name for f in archive_iceberg.SCHEMA.fields])
    table.append(
        df.to_arrow(),
        snapshot_properties={"manifest_fingerprint": fingerprint, "uri_count": "1"},
    )
    manifest.mark_iceberg_committed([uri])


def _read_raw_parquet(uri: str) -> pl.DataFrame:
    import pyarrow as pa
    import pyarrow.parquet as pq

    fs = garage_pyarrow_fs()
    # ParquetFile, not the dataset API: multi-row-group files trip dataset schema
    # merging on dictionary-encoded constant columns.
    table = pq.ParquetFile(fs.open_input_file(uri[len("s3://"):])).read()
    decoded = {}
    for name in table.column_names:
        col = table.column(name)
        if pa.types.is_dictionary(col.type):
            col = col.cast(col.type.value_type)
        decoded[name] = col
    return pl.from_arrow(pa.table(decoded))


def _optimize_archive() -> None:
    try:
        import trino

        conn = trino.dbapi.connect(
            host="trino-coordinator", port=8080, user="root", catalog="iceberg", http_scheme="http"
        )
        cur = conn.cursor()
        cur.execute(f"ALTER TABLE iceberg.{archive_iceberg.QUALIFIED} EXECUTE optimize")
        cur.fetchall()
        print("optimize done")
    except Exception as exc:  # noqa: BLE001 — wave data is committed; compaction can rerun via maintain_iceberg_states
        print(f"WARNING: optimize skipped ({exc}); the daily maintenance lane will compact instead")


def run_states(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    table = archive_iceberg.ensure_archive_table()
    known = _manifest_uris("%/bronze/archive\\_states\\_raw/%")
    bucket = get_bucket()

    failures: list[str] = []
    day = start
    while day <= end:
        key = f"bronze/archive_states_raw/dt={day.isoformat()}/source=adsblol/part-000.parquet"
        uri = f"s3://{bucket}/{key}"
        if known.get(uri):
            print(f"{day}: already committed, skipping")
            day += timedelta(days=1)
            continue

        # One bad day (corrupt tar, transient network, quality gate) must not
        # abort the rest of the wave — record it and move on; reruns retry it.
        try:
            was_pending = uri in known
            if was_pending:
                # Pending = a prior run crashed after landing the parquet; reuse it
                # instead of re-streaming the multi-GB tar.
                df = _read_raw_parquet(uri)
                print(f"{day}: reusing pending raw parquet ({df.height} rows)")
            else:
                df = _day_rows(day, args.min_traces)
                if df is None:
                    failures.append(f"{day} (no release found)")
                    print(f"{day}: no release found, skipping")
                    day += timedelta(days=1)
                    continue
                write_parquet(df, key)
                epochs = df.get_column("snapshot_time")
                manifest.record_load(
                    object_uri=uri,
                    snapshot_min=int(epochs.min()) if df.height else None,
                    snapshot_max=int(epochs.max()) if df.height else None,
                    row_count=df.height,
                )
            _append_day(table, uri, df, allow_recovery=was_pending)
        except Exception as exc:  # noqa: BLE001 — failure is recorded; the wave continues
            failures.append(f"{day} ({exc})")
            print(f"{day}: FAILED — {exc}")
        day += timedelta(days=1)

    _optimize_archive()
    if failures:
        print(f"missing releases for: {', '.join(failures)}")
    # tag:history+ (with the graph operator) so the chart-facing union mart rebuilds too;
    # the 12-min transform tick achieves the same eventually.
    print("next: dbt run --select tag:history+ (or wait for the next transform tick)")
    return 1 if failures else 0


def run_flights(args: argparse.Namespace) -> int:
    from include.opensky_client import OpenSkyClient

    client = OpenSkyClient.from_env()
    if not client.is_authenticated:
        print("OPENSKY_CLIENT_ID/SECRET missing — historical /flights needs auth")
        return 1

    airports = [a.strip().upper() for a in args.airports.split(",") if a.strip()]
    until = date.fromisoformat(args.until)
    from_day = date.fromisoformat(args.from_date)
    known = _manifest_uris("%/bronze/flights\\_raw/backfill/%")
    bucket = get_bucket()
    now_iso = datetime.now(timezone.utc).isoformat()

    calls = 0
    stopped = False
    for w_begin, begin_ts, end_ts in ab.flights_windows(until, from_day):
        for icao in airports:
            key = f"bronze/flights_raw/backfill/dt={w_begin.isoformat()}/airport={icao}.parquet"
            uri = f"s3://{bucket}/{key}"
            if uri in known:
                continue
            if client.last_credits_remaining is not None and client.last_credits_remaining < args.reserve:
                stopped = True
                break
            if args.max_calls and calls >= args.max_calls:
                stopped = True
                break

            rows: list[dict] = []
            for direction, fetch in (
                ("arrival", client.get_flights_arrival),
                ("departure", client.get_flights_departure),
            ):
                for f in fetch(icao, begin_ts, end_ts):
                    # first_seen is the partition key + dedup key; drop records OpenSky returns without it.
                    if f.get("firstSeen") is None:
                        continue
                    rows.append(flight_row(f, icao, direction, window_kind="backfill"))
                calls += 1

            df = pl.DataFrame(rows, schema=RAW_FLIGHTS_SCHEMA).with_columns(
                pl.lit(now_iso).alias("ingested_at"),
            )
            write_parquet(df, key)
            first_seens = [r["first_seen"] for r in rows if r["first_seen"]]
            manifest.record_load(
                object_uri=uri,
                snapshot_min=min(first_seens) if first_seens else None,
                snapshot_max=max(first_seens) if first_seens else None,
                row_count=df.height,
            )
            print(
                f"{w_begin} {icao}: {df.height} flights "
                f"(credits remaining: {client.last_credits_remaining})"
            )
        if stopped:
            break

    print(f"done: {calls} calls this run" + (" (budget/limit reached — rerun tomorrow)" if stopped else ""))
    # Manifest inserts from outside Airflow emit no asset event; trigger the drain.
    if calls:
        try:
            subprocess.run(["airflow", "dags", "trigger", "tableize_flights"], check=True, capture_output=True)
            print("triggered tableize_flights")
        except Exception as exc:  # noqa: BLE001 — rows stay pending; the next daily ingest cycle drains them
            print(f"WARNING: could not trigger tableize_flights ({exc}); pending rows drain on the next daily cycle")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="v5.2 one-shot history backfill")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_states = sub.add_parser("states", help="adsb.lol globe_history -> bronze.archive_states")
    p_states.add_argument("--start", required=True, help="first day, YYYY-MM-DD")
    p_states.add_argument("--end", required=True, help="last day inclusive, YYYY-MM-DD")
    p_states.add_argument("--min-traces", type=int, default=10_000,
                          help="quality-gate floor; lower deliberately for known-partial upstream days")
    p_states.set_defaults(func=run_states)

    p_flights = sub.add_parser("flights", help="OpenSky REST /flights history -> v5.1 flights lane")
    # Required, not defaulted: the right value is deploy-specific (the day before
    # this deployment's live flights lane began — for ours, 2026-06-05).
    p_flights.add_argument("--until", required=True, help="newest day to fill, YYYY-MM-DD")
    p_flights.add_argument("--from-date", default="2020-01-01", help="oldest day to fill, YYYY-MM-DD")
    p_flights.add_argument("--airports", default="RJTT,RJAA", help="comma-separated ICAO codes")
    p_flights.add_argument("--reserve", type=int, default=500,
                           help="stop when the credit bucket drops below this")
    p_flights.add_argument("--max-calls", type=int, default=0, help="optional hard cap per run")
    p_flights.set_defaults(func=run_flights)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
