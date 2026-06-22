from __future__ import annotations

import logging
import os
import re
from typing import Iterable, Optional

import sqlalchemy as sa

from include.db import analytics_engine

log = logging.getLogger(__name__)


def _safe_identifier(value: str) -> str:
    # _CH_DB is interpolated into SQL as a bare identifier; reject anything that isn't one.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return value


# P1 named collection: Garage creds via from_env (no inline secrets); its url carries the bucket.
_GARAGE_COLLECTION = "garage"
_CH_DB = _safe_identifier(os.environ.get("CLICKHOUSE_DB", "bronze"))

_STATES_PREFIXES = ("bronze/states_raw",)
_FLIGHTS_PREFIXES = ("bronze/flights_raw",)
_ARCHIVE_PREFIXES = ("bronze/archive_states_raw",)
# Cap files per INSERT so a pre-backfill drain can't OOM and each batch stays one part (no "too many parts").
_DEFAULT_BATCH_FILES = 1000
# adsb rows-per-file is ~40× a states region file, so use a much smaller batch to bound memory.
_ADSB_BATCH_FILES = 24


def ch_client():
    # Lazy import so this module is importable (and unit-testable) without clickhouse-connect.
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=_CH_DB,
    )


def insert_arrow_best_effort(table: str, arrow_table, *, settings=None) -> tuple[bool, int]:
    # Best-effort: a ClickHouse failure must never red the tableize DAG, so this swallows all.
    try:
        client = ch_client()
        try:
            client.insert_arrow(table, arrow_table, settings=settings or {})
        finally:
            client.close()
        return True, arrow_table.num_rows
    except Exception:
        log.exception("CH dual-write to %s failed (non-fatal)", table)
        return False, 0


def command_best_effort(sql: str, *, settings=None) -> bool:
    try:
        client = ch_client()
        try:
            client.command(sql, settings=settings or {})
        finally:
            client.close()
        return True
    except Exception:
        log.exception("CH command failed (non-fatal)")
        return False


def _chunks(seq: list, size: Optional[int]):
    if not size or size >= len(seq):
        yield seq
        return
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _safe(label: str, fn):
    # Total non-blocking guard: even a Postgres hiccup or transform bug must never red the tableize DAG.
    try:
        return fn()
    except Exception:
        log.exception("CH %s failed (non-fatal)", label)
        return {"ch_loaded": 0, "files": 0, "ok": False}


def _drain_transformed(prefixes: Iterable[str], transform, ch_table: str, *,
                       batch_files: Optional[int], engine) -> dict:
    # Re-read raw Parquet and apply the shared bronze transform (include/bronze_transforms.py) — never a SQL re-impl.
    import polars as pl

    from include import manifest
    from include.s3_helpers import garage_pyarrow_fs, read_pending_frames

    pending: list[dict] = []
    for prefix in prefixes:
        pending.extend(manifest.pending_ch_uris(prefix, engine))
    if not pending:
        return {"ch_loaded": 0, "files": 0, "ok": True}

    fs = garage_pyarrow_fs()
    loaded_rows = loaded_files = 0
    all_ok = True
    for batch in _chunks(pending, batch_files):
        try:
            frames = read_pending_frames(fs, batch)
            df = transform(pl.concat(frames, how="diagonal_relaxed"))
        except Exception:
            log.exception("CH %s: read/transform failed for a %d-file batch (skipped)", ch_table, len(batch))
            all_ok = False
            continue
        ok, rows = insert_arrow_best_effort(ch_table, df.to_arrow())
        if ok:
            # Best-effort gap (hardened in P8): INSERT+mark aren't atomic — a crash here can dup on retry.
            manifest.mark_ch_loaded([r["object_uri"] for r in batch], engine)
            loaded_rows += rows
            loaded_files += len(batch)
        else:
            all_ok = False
    return {"ch_loaded": loaded_rows, "files": loaded_files, "ok": all_ok}


def load_states_pending_to_ch(engine: Optional[sa.Engine] = None, *,
                              prefixes: Iterable[str] = _STATES_PREFIXES,
                              batch_files: Optional[int] = _DEFAULT_BATCH_FILES) -> dict:
    from include import bronze_transforms as bt

    return _safe("states load", lambda: _drain_transformed(
        prefixes, bt.transform_states_frame, "opensky_states", batch_files=batch_files, engine=engine))


def load_flights_pending_to_ch(engine: Optional[sa.Engine] = None, *,
                               batch_files: Optional[int] = _DEFAULT_BATCH_FILES) -> dict:
    from include import bronze_transforms as bt

    return _safe("flights load", lambda: _drain_transformed(
        _FLIGHTS_PREFIXES, bt.transform_flights_frame, "opensky_flights",
        batch_files=batch_files, engine=engine))


def load_adsb_pending_to_ch(engine: Optional[sa.Engine] = None, *,
                            batch_files: Optional[int] = _ADSB_BATCH_FILES) -> dict:
    return _safe("adsb load", lambda: _load_adsb_pending_to_ch(engine, batch_files))


def _read_adsb_table(fs, s3_uri: str):
    import pyarrow.parquet as pq

    # Read via an open handle (not the path) so pyarrow doesn't add the hive `dt=` partition column,
    # which the byte-mirror table doesn't have. CH's own s3() can't read explicit Garage keys (only
    # wildcard globs that list), so the per-tick lane reads with pyarrow, not s3().
    with fs.open_input_file(s3_uri[len("s3://"):]) as fh:
        return pq.read_table(fh)


def _load_adsb_pending_to_ch(engine: Optional[sa.Engine], batch_files: Optional[int]) -> dict:
    import pyarrow as pa

    from include import adsb_manifest as am
    from include.s3_helpers import garage_pyarrow_fs

    pending = am.pending_ch_adsb_uris(engine)
    if not pending:
        return {"ch_loaded": 0, "files": 0, "ok": True}
    fs = garage_pyarrow_fs()
    loaded_rows = loaded_files = 0
    all_ok = True
    for batch in _chunks(pending, batch_files):
        tables, good = [], []
        for p in batch:
            try:
                tables.append(_read_adsb_table(fs, p["s3_uri"]))
                good.append(p)
            except Exception:
                log.exception("CH adsb: read failed for %s (skipped)", p["filename"])
                all_ok = False
        if not tables:
            continue
        ok, rows = insert_arrow_best_effort("adsb_states", pa.concat_tables(tables, promote_options="default"))
        if ok:
            # Best-effort gap (hardened in P8): INSERT+mark aren't atomic — a crash here can dup on retry.
            am.mark_ch_loaded([p["filename"] for p in good], engine)
            loaded_rows += rows
            loaded_files += len(good)
        else:
            all_ok = False
    return {"ch_loaded": loaded_rows, "files": loaded_files, "ok": all_ok}


# --- one-time backfill (run once at merge via scripts/ch_backfill_bronze.sh) ----------------

def reset_ch_bronze() -> None:
    # Make the backfill re-runnable: wipe the CH tables + clear markers so a re-run reloads from scratch.
    from include import adsb_manifest as am
    from include import manifest

    # Self-migrate the ch_loaded_at columns before touching them (DAGs may not have run yet).
    manifest.ensure_table()
    am.ensure_table()
    # TRUNCATE must succeed before clearing markers (else a re-INSERT dups); raise here — it's a one-shot.
    client = ch_client()
    try:
        for table in ("adsb_states", "opensky_states", "opensky_flights"):
            client.command(f"TRUNCATE TABLE IF EXISTS {_CH_DB}.{table}")
    finally:
        client.close()
    eng = analytics_engine()
    with eng.begin() as conn:
        conn.execute(sa.text("UPDATE public.ingestion_manifest SET ch_loaded_at = NULL"))
        conn.execute(sa.text("UPDATE public.adsb_ingestion_manifest SET ch_loaded_at = NULL"))


def backfill_adsb() -> dict:
    # One recursive sweep of the partitioned tree (spike ~12s/19.2M), then mark all so the per-tick no-ops.
    from include import adsb_manifest as am

    sql = (
        f"INSERT INTO {_CH_DB}.adsb_states "
        f"SELECT * FROM s3({_GARAGE_COLLECTION}, filename='bronze/adsb_state/**/*.parquet', format='Parquet')"
    )
    if not command_best_effort(sql):
        return {"ok": False, "marked": 0}
    marked = am.mark_ch_loaded([p["filename"] for p in am.pending_ch_adsb_uris()])
    return {"ok": True, "marked": marked}


def backfill_states(batch_files: int = 500) -> dict:
    # Include the legacy bronze/states lane (pre-states_raw history) so CH carries the full states history.
    return load_states_pending_to_ch(prefixes=("bronze/states", "bronze/states_raw"), batch_files=batch_files)


def backfill_flights(batch_files: int = 500) -> dict:
    return load_flights_pending_to_ch(batch_files=batch_files)


def run_backfill(reset: bool = True) -> dict:
    if reset:
        reset_ch_bronze()
        adsb = backfill_adsb()          # table just truncated → full s3() sweep is safe
    else:
        adsb = load_adsb_pending_to_ch()  # resume: pending-only, never re-sweeps (no dup on MergeTree)
    return {"adsb": adsb, "states": backfill_states(), "flights": backfill_flights()}


# --- P3 marts lane one-time setup: aircraft_db bronze load + hex-country dict reload ----------

_AIRCRAFT_DB_PREFIX = "bronze/aircraft_db_raw"
# Coerce blank cells ('' -> NULL) so CH bronze.aircraft_db keeps the registry joins clean (no empty-string keys).
_AIRCRAFT_DB_STR_COLS = (
    "icao24", "registration", "manufacturericao", "manufacturername", "model", "typecode",
    "serialnumber", "icaoaircrafttype", "operator", "operatorcallsign", "operatoricao", "owner",
)
# Explicit s3() structure (the landed Parquet's columns, all written as strings by ingest_aircraft_db) so CH
# never has to INFER the schema from the files — without it, a fresh/empty Garage glob raises error 636
# (CANNOT_EXTRACT_TABLE_STRUCTURE) instead of returning zero rows, which would break the first-deploy bootstrap.
_AIRCRAFT_DB_S3_STRUCTURE = ", ".join(
    f"{c} Nullable(String)" for c in _AIRCRAFT_DB_STR_COLS + ("as_of_date", "ingested_at")
)


def reload_hex_country_dict() -> bool:
    # reg_country lookups need the P1 range_hashed dict loaded immediately after a fresh seed (else lazy-load lag).
    return command_best_effort("SYSTEM RELOAD DICTIONARY dim.dict_hex_country")


def backfill_aircraft_db() -> dict:
    # Rebuild bronze.aircraft_db from the Garage Parquet glob (weekly + at setup). Non-destructive: load a
    # staging table, then atomic EXCHANGE only if non-empty — a failed reload keeps the last-good registry serving.
    nulled = ", ".join(f"nullIf({c}, '')" for c in _AIRCRAFT_DB_STR_COLS)
    stage = f"{_CH_DB}.aircraft_db_reload"
    sql = (
        f"INSERT INTO {stage} "
        f"(icao24, registration, manufacturericao, manufacturername, model, typecode, serialnumber, "
        f"icaoaircrafttype, operator, operatorcallsign, operatoricao, owner, as_of_date, ingested_at, "
        f"committed_at) "
        f"SELECT {nulled}, toDateOrNull(as_of_date), "
        f"parseDateTime64BestEffortOrNull(ingested_at), parseDateTime64BestEffortOrNull(ingested_at) "
        f"FROM s3({_GARAGE_COLLECTION}, filename='{_AIRCRAFT_DB_PREFIX}/**/*.parquet', format='Parquet', "
        f"structure='{_AIRCRAFT_DB_S3_STRUCTURE}')"
    )
    # Inline guard (not _safe): keep the {ok, rows} shape on both paths so callers never KeyError on failure.
    try:
        client = ch_client()
        try:
            client.command(f"CREATE TABLE IF NOT EXISTS {stage} AS {_CH_DB}.aircraft_db")
            client.command(f"TRUNCATE TABLE {stage}")
            client.command(sql)
            rows = client.query(f"SELECT count() FROM {stage}").result_rows[0][0]
            if int(rows) == 0:
                # Empty glob = nothing to load (e.g. a fresh deploy before any registry has landed): a success
                # no-op, not a failure — keep the existing table and DON'T clobber it with an empty reload.
                # ok=True so the weekly ingest task / bootstrap don't red on "no data yet"; a real CH/Garage
                # error falls through to the except below (ok=False).
                log.warning("CH aircraft_db reload produced 0 rows — keeping the existing table")
                return {"ok": True, "rows": 0, "skipped": True}
            # EXCHANGE is atomic on the Atomic database engine (bronze): readers see old-or-new, never empty.
            client.command(f"EXCHANGE TABLES {_CH_DB}.aircraft_db AND {stage}")
        finally:
            client.close()
        return {"ok": True, "rows": int(rows)}
    except Exception:
        log.exception("CH aircraft_db load failed (non-fatal)")
        return {"ok": False, "rows": 0}


def transform_archive_frame(df):
    # Reuse the shared states transform, re-attaching `source` (transform_states_frame drops it to the state cols).
    from include import bronze_transforms as bt

    return bt.transform_states_frame(df).with_columns(df.get_column("source").alias("source"))


def _read_archive_frame(fs, uri: str):
    # Open-handle read avoids the `source=` hive-partition inference colliding with the file's own `source` column.
    import polars as pl
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(fs.open_input_file(uri[len("s3://"):]))
    # Decode dict columns first: a `source` that is dict in some files but plain string in others breaks the concat.
    decoded = {}
    for name in table.column_names:
        col = table.column(name)
        if pa.types.is_dictionary(col.type):
            col = col.cast(col.type.value_type)
        decoded[name] = col
    return pl.from_arrow(pa.table(decoded))


def _load_archive_pending_to_ch(engine: Optional[sa.Engine], batch_files: Optional[int]) -> dict:
    import polars as pl

    from include import manifest
    from include.s3_helpers import garage_pyarrow_fs

    pending = manifest.pending_ch_uris(_ARCHIVE_PREFIXES[0], engine)
    if not pending:
        return {"ch_loaded": 0, "files": 0, "ok": True}
    fs = garage_pyarrow_fs()
    loaded_rows = loaded_files = 0
    all_ok = True
    for batch in _chunks(pending, batch_files):
        frames, good = [], []
        for row in batch:
            try:
                frames.append(_read_archive_frame(fs, row["object_uri"]))
                good.append(row)
            except Exception:
                log.exception("CH archive: read failed for %s (skipped)", row["object_uri"])
                all_ok = False
        if not frames:
            continue
        try:
            df = transform_archive_frame(pl.concat(frames, how="diagonal_relaxed"))
        except Exception:
            log.exception("CH archive: read/transform failed for a %d-file batch (skipped)", len(frames))
            all_ok = False
            continue
        ok, rows = insert_arrow_best_effort("archive_states", df.to_arrow())
        if ok:
            # Best-effort gap (hardened in P8): INSERT+mark aren't atomic — a crash here can dup on retry.
            manifest.mark_ch_loaded([r["object_uri"] for r in good], engine)
            loaded_rows += rows
            loaded_files += len(good)
        else:
            all_ok = False
    return {"ch_loaded": loaded_rows, "files": loaded_files, "ok": all_ok}


def load_archive_pending_to_ch(engine: Optional[sa.Engine] = None, *,
                               batch_files: Optional[int] = 1) -> dict:
    # The frozen archive backfill feeds the P3b history marts (stg_states_history -> agg_hourly_traffic).
    # batch_files=1: each daily archive part is full-resolution (~2.6M rows), so drain one file per INSERT.
    return _safe("archive load", lambda: _load_archive_pending_to_ch(engine, batch_files))


def backfill_archive_states(batch_files: int = 1) -> dict:
    # P2 skips the frozen archive lane; one-time idempotent load (truncate + clear markers, then re-drain).
    from include import manifest

    try:
        client = ch_client()
        try:
            client.command(f"TRUNCATE TABLE IF EXISTS {_CH_DB}.archive_states")
        finally:
            client.close()
        manifest.ensure_table()
        with analytics_engine().begin() as conn:
            conn.execute(sa.text(
                r"UPDATE public.ingestion_manifest SET ch_loaded_at = NULL "
                r"WHERE object_uri LIKE '%/bronze/archive\_states\_raw/%'"))
    except Exception:
        log.exception("CH archive_states reset failed (non-fatal)")
        return {"ok": False, "ch_loaded": 0, "files": 0}
    return load_archive_pending_to_ch(batch_files=batch_files)


def backfill_live_archive_history() -> dict:
    # One-time idempotent gap-fill of the accumulator's pre-window hours from CH bronze (the cold-start hole).
    sql = (
        "INSERT INTO gold_ch.agg_hourly_traffic_live_archive "
        "WITH deduped AS ("
        "  SELECT icao24, on_ground, velocity AS velocity_mps, toStartOfHour(snapshot_time) AS snapshot_hour "
        "  FROM (SELECT icao24, on_ground, velocity, snapshot_time, "
        "               row_number() OVER (PARTITION BY icao24, snapshot_time ORDER BY ingested_at DESC) AS rn "
        f"        FROM {_CH_DB}.opensky_states "
        "        WHERE latitude BETWEEN 20 AND 50 AND longitude BETWEEN 122 AND 165) WHERE rn = 1) "
        "SELECT snapshot_hour, uniqExact(icao24), count(*), "
        "       sum(case when not on_ground then 1 else 0 end), "
        "       sum(case when on_ground then 1 else 0 end), "
        "       cast(avg(case when not on_ground then velocity_mps * 3.6 end) as decimal(10, 2)) "
        "FROM deduped GROUP BY snapshot_hour "
        "HAVING snapshot_hour < (SELECT min(snapshot_hour) FROM gold_ch.agg_hourly_traffic_live_archive) "
        "   AND snapshot_hour < toStartOfHour(now('UTC')) - INTERVAL 2 HOUR"
    )
    try:
        client = ch_client()
        try:
            if not client.query("EXISTS TABLE gold_ch.agg_hourly_traffic_live_archive").result_rows[0][0]:
                log.info("CH live_archive not built yet — skip the history seed (run after the first transform_marts)")
                return {"ok": True, "seeded_hours": 0, "skipped": True}
            before = client.query("SELECT count() FROM gold_ch.agg_hourly_traffic_live_archive").result_rows[0][0]
            client.command(sql)
            after = client.query("SELECT count() FROM gold_ch.agg_hourly_traffic_live_archive").result_rows[0][0]
        finally:
            client.close()
        return {"ok": True, "seeded_hours": int(after) - int(before)}
    except Exception:
        log.exception("CH live_archive history seed failed (non-fatal)")
        return {"ok": False, "seeded_hours": 0}


def setup_marts() -> dict:
    # One-time at P3 deploy: the non-dbt pieces (dims come via `dbt seed --target clickhouse`).
    return {
        "aircraft_db": backfill_aircraft_db(),
        "archive_states": backfill_archive_states(),
        "dict_reloaded": reload_hex_country_dict(),
    }


def bootstrap_marts() -> dict:
    # Fresh-deploy CH marts bootstrap (clickhouse-marts-init): reload the hex-country dict + load the aircraft
    # registry. RAISES if a critical step errors so the Compose one-shot fails loudly (an empty registry glob on
    # a brand-new deploy is ok=True, a no-op — only a real CH/Garage error trips this). dbt seed runs separately.
    dict_ok = reload_hex_country_dict()
    aircraft = backfill_aircraft_db()
    if not dict_ok or not aircraft.get("ok"):
        raise RuntimeError(f"CH marts bootstrap failed: dict_reloaded={dict_ok} aircraft_db={aircraft}")
    return {"dict_reloaded": dict_ok, "aircraft_db": aircraft}


if __name__ == "__main__":
    import json
    import sys

    if "--marts" in sys.argv:
        out = setup_marts()
    elif "--seed-live-archive" in sys.argv:
        out = backfill_live_archive_history()
    else:
        out = run_backfill(reset="--no-reset" not in sys.argv)
    print(json.dumps(out, default=str))
