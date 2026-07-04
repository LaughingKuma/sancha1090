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
_ADSBLOL_PREFIXES = ("bronze/adsblol_states_raw",)
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
        # read_pending_frames skips a file missing from Garage (returns only the readable ones) so one phantom
        # can't wedge the batch; a skip means not every file was read, so the lane isn't fully ok this pass.
        frames, good = read_pending_frames(fs, batch)
        if len(good) != len(batch):
            all_ok = False
        if not frames:
            continue
        try:
            df = transform(pl.concat(frames, how="diagonal_relaxed"))
        except Exception:
            log.exception("CH %s: transform failed for a %d-file batch (skipped)", ch_table, len(good))
            all_ok = False
            continue
        ok, rows = insert_arrow_best_effort(ch_table, df.to_arrow())
        if ok:
            # INSERT+mark aren't atomic, so a crash here re-inserts the batch on retry. Harmless now: the states
            # lane (opensky_states) is ReplacingMergeTree keyed on a committed_at-free fingerprint, so a replay
            # collapses on merge; the flights lane lands single-block and fact_flights grain-dedups downstream.
            manifest.mark_ch_loaded([r["object_uri"] for r in good], engine)
            loaded_rows += rows
            loaded_files += len(good)
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


def _bake_adsb_flags(table):
    # v6.3: decode the dbFlags integer from the verbatim _raw_json and drop the blob (eliminated from CH — it was
    # ~39% of the table and the only CH readers decoded dbFlags). absent/null/non-object/malformed -> 0, the same
    # 2-valued contract JSONExtractInt gives. insert_arrow maps by NAME and rejects an extra arrow column, so the
    # _raw_json column is projected out (not merely absent from the target).
    import json

    import pyarrow as pa

    def _flag(raw):
        if not raw:
            return 0
        try:
            v = json.loads(raw).get("dbFlags", 0)
        except (ValueError, TypeError, AttributeError):
            return 0
        try:
            return int(v) if v is not None else 0
        except (ValueError, TypeError):
            return 0

    flags = pa.array([_flag(r) for r in table.column("_raw_json").to_pylist()], type=pa.int32())
    kept = [c for c in table.column_names if c != "_raw_json"]
    return table.select(kept).append_column("db_flags", flags)


def _read_adsb_table(fs, s3_uri: str):
    import pyarrow.parquet as pq

    # Read via an open handle (not the path) so pyarrow doesn't add the hive `dt=` partition column,
    # which the byte-mirror table doesn't have. CH's own s3() can't read explicit Garage keys (only
    # wildcard globs that list), so the per-tick lane reads with pyarrow, not s3(). Bake db_flags + drop _raw_json.
    with fs.open_input_file(s3_uri[len("s3://"):]) as fh:
        return _bake_adsb_flags(pq.read_table(fh))


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
            # INSERT+mark aren't atomic, so a crash here re-inserts on retry. v6.3 made adsb_states a
            # ReplacingMergeTree keyed on (capture_ts, hex) == unique content, so a replayed batch collapses on
            # merge (the re-inserted rows are byte-identical to the originals); the per-batch marker bounds the rest.
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


class StrayObjectError(RuntimeError):
    """Garage objects under the ADS-B prefix that no manifest row claims — refuse to rebuild from them."""


def rebuild_adsb_from_garage(target_table: str = "adsb_states", mark: bool = True) -> dict:
    # One recursive sweep of the partitioned tree (spike ~12s/19.2M), then mark all so the per-tick no-ops. v6.3:
    # the source Parquet carries _raw_json (60 cols) but the CH table now carries the baked db_flags instead, so
    # the SELECT can't stay SELECT * — project the passthrough cols + decode db_flags from the source _raw_json.
    # target_table lets the migration build adsb_states_new from source. mark=False for a SCRATCH build (the data
    # is not in the LIVE table until the swap, so advancing the manifest would strand files if the build aborts or
    # is --build-only, and could mark a file that landed mid-sweep without being included): leave the files
    # pending and let the per-tick loader replay them after the swap — RMT collapses the byte-identical re-insert.
    # (No explicit structure= 636-hardening here — this reload sweep only ever runs on a non-empty glob.)
    from include import adsb_manifest as am
    from include.adsb_schema import CH_ADSB_COLUMNS

    from include.s3_helpers import get_bucket, get_s3fs

    # Membership guard: the s3() sweep ingests ANYTHING under the prefix, and a rebuild is exactly when
    # nobody is watching. Manifest snapshot AFTER the listing narrows the just-landed race; on a false
    # positive (file landed mid-listing), wait for ingest_adsb to register it and re-run.
    fs = get_s3fs()
    listed = {f"s3://{k}" for k in fs.find(f"{get_bucket()}/bronze/adsb_state/")
              if k.endswith(".parquet")}
    strays = sorted(listed - am.all_adsb_state_uris())
    if strays:
        raise StrayObjectError(
            f"{len(strays)} unregistered parquet(s) under bronze/adsb_state/: {strays[:10]}")

    def q(c):
        return f"`{c}`" if c == "desc" else c  # `desc` is reserved; every other adsb col is a plain identifier

    target = _safe_identifier(target_table)
    cols = ", ".join(q(c) for c in CH_ADSB_COLUMNS)
    select = ", ".join(
        "toInt32(JSONExtractInt(coalesce(_raw_json, ''), 'dbFlags'))" if c == "db_flags" else q(c)
        for c in CH_ADSB_COLUMNS
    )
    sql = (
        f"INSERT INTO {_CH_DB}.{target} ({cols}) "
        f"SELECT {select} FROM s3({_GARAGE_COLLECTION}, "
        f"filename='bronze/adsb_state/**/*.parquet', format='Parquet')"
    )
    # Snapshot the pending set BEFORE the sweep: a file landing mid-sweep is recorded pending but may miss the
    # glob, so marking the POST-sweep pending set could mark-without-loading it. Marking only the pre-sweep set
    # leaves any mid-sweep arrival pending for the per-tick loader to replay (RMT-safe). mark=False marks nothing.
    pending = am.pending_ch_adsb_uris() if mark else []
    if not command_best_effort(sql):
        return {"ok": False, "marked": 0}
    marked = am.mark_ch_loaded([p["filename"] for p in pending]) if mark else 0
    return {"ok": True, "marked": marked}


def backfill_states(batch_files: int = 500) -> dict:
    # Include the legacy bronze/states lane (pre-states_raw history) so CH carries the full states history.
    return load_states_pending_to_ch(prefixes=("bronze/states", "bronze/states_raw"), batch_files=batch_files)


def backfill_flights(batch_files: int = 500) -> dict:
    return load_flights_pending_to_ch(batch_files=batch_files)


def run_backfill(reset: bool = True) -> dict:
    if reset:
        reset_ch_bronze()
        adsb = rebuild_adsb_from_garage()  # table just truncated → full s3() sweep is safe
    else:
        adsb = load_adsb_pending_to_ch()  # resume: pending-only, never re-sweeps (no dup on MergeTree)
    return {"adsb": adsb, "states": backfill_states(), "flights": backfill_flights()}


def optimize_states_final(table: str = "opensky_states") -> dict:
    # Force the ReplacingMergeTree dedup merge so a crash-replay surplus can't accumulate physically — CH merges
    # are async and a part "may stay unmerged indefinitely" (CH docs), and the exact content-fp gate reads
    # logical truth (distinct content) so it wouldn't see the bloat. Two guards keep this from being wasteful:
    #   - optimize_skip_merged_partitions=1 so an already-single-part partition is NOT rewritten every run (an
    #     unrestricted OPTIMIZE FINAL re-rewrites the whole table daily); only multi-part partitions (where a
    #     replay part landed, or background merges haven't caught up) are merged.
    #   - skip entirely until the table is actually ReplacingMergeTree — pre-migration it's plain MergeTree, where
    #     OPTIMIZE does no dedup and would only churn parts.
    # RAISES on a real failure so the daily maintain_bronze_dedup DAG reds if dedup stalls.
    table = _safe_identifier(table)
    client = ch_client()
    try:
        rows = client.query(
            f"SELECT engine FROM system.tables WHERE database = '{_CH_DB}' AND name = '{table}'"
        ).result_rows
        engine = rows[0][0] if rows else ""
        if not engine.startswith("ReplacingMergeTree"):
            return {"optimized": False, "skipped": True, "engine": engine}
        client.command(
            f"OPTIMIZE TABLE {_CH_DB}.{table} FINAL SETTINGS optimize_skip_merged_partitions = 1"
        )
    finally:
        client.close()
    return {"optimized": True, "skipped": False}


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


def transform_adsblol_frame(df):
    # Reuse the shared states transform, re-attaching `source` (transform_states_frame drops it to the state cols).
    from include import bronze_transforms as bt

    return bt.transform_states_frame(df).with_columns(df.get_column("source").alias("source"))


def _read_adsblol_frame(fs, uri: str):
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


def _load_adsblol_pending_to_ch(engine: Optional[sa.Engine], batch_files: Optional[int]) -> dict:
    import polars as pl

    from include import manifest
    from include.s3_helpers import garage_pyarrow_fs

    pending = manifest.pending_ch_uris(_ADSBLOL_PREFIXES[0], engine)
    if not pending:
        return {"ch_loaded": 0, "files": 0, "ok": True}
    fs = garage_pyarrow_fs()
    loaded_rows = loaded_files = 0
    all_ok = True
    for batch in _chunks(pending, batch_files):
        frames, good = [], []
        for row in batch:
            try:
                frames.append(_read_adsblol_frame(fs, row["object_uri"]))
                good.append(row)
            except Exception:
                log.exception("CH adsblol: read failed for %s (skipped)", row["object_uri"])
                all_ok = False
        if not frames:
            continue
        try:
            df = transform_adsblol_frame(pl.concat(frames, how="diagonal_relaxed"))
        except Exception:
            log.exception("CH adsblol: read/transform failed for a %d-file batch (skipped)", len(frames))
            all_ok = False
            continue
        ok, rows = insert_arrow_best_effort("adsblol_states", df.to_arrow())
        if ok:
            # INSERT+mark aren't atomic. adsblol_states stays plain MergeTree (frozen, exact today, and a
            # states-style fingerprint can't apply — see 01_bronze.sql), so its idempotency is the truncate-first
            # reload (rebuild_adsblol_states), NOT a bare resume after a torn multi-block insert.
            manifest.mark_ch_loaded([r["object_uri"] for r in good], engine)
            loaded_rows += rows
            loaded_files += len(good)
        else:
            all_ok = False
    return {"ch_loaded": loaded_rows, "files": loaded_files, "ok": all_ok}


def load_adsblol_pending_to_ch(engine: Optional[sa.Engine] = None, *,
                               batch_files: Optional[int] = 1) -> dict:
    # The frozen adsblol backfill feeds the P3b history marts (stg_states_adsblol -> agg_hourly_traffic).
    # batch_files=1: each daily adsblol part is full-resolution (~2.6M rows), so drain one file per INSERT.
    return _safe("adsblol load", lambda: _load_adsblol_pending_to_ch(engine, batch_files))


def rebuild_adsblol_states(batch_files: int = 1) -> dict:
    # P2 skips the frozen adsblol lane; one-time idempotent load (truncate + clear markers, then re-drain).
    from include import manifest

    try:
        client = ch_client()
        try:
            client.command(f"TRUNCATE TABLE IF EXISTS {_CH_DB}.adsblol_states")
        finally:
            client.close()
        manifest.ensure_table()
        with analytics_engine().begin() as conn:
            conn.execute(sa.text(
                r"UPDATE public.ingestion_manifest SET ch_loaded_at = NULL "
                r"WHERE object_uri LIKE '%/bronze/adsblol\_states\_raw/%'"))
    except Exception:
        log.exception("CH adsblol_states reset failed (non-fatal)")
        return {"ok": False, "ch_loaded": 0, "files": 0}
    return load_adsblol_pending_to_ch(batch_files=batch_files)


def setup_marts() -> dict:
    # One-time at P3 deploy: the non-dbt pieces (dims come via `dbt seed --target clickhouse`).
    return {
        "aircraft_db": backfill_aircraft_db(),
        "adsblol_states": rebuild_adsblol_states(),
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
    else:
        out = run_backfill(reset="--no-reset" not in sys.argv)
    print(json.dumps(out, default=str))
