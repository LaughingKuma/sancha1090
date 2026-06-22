from __future__ import annotations

import re
from pathlib import Path

from include import ch_parity as p
from include.bronze_transforms import STATES_COLUMNS

BRONZE_SQL = Path(__file__).resolve().parents[1] / "clickhouse" / "sql" / "01_bronze.sql"


def _table_ddl(table: str) -> str:
    # The full CREATE block for one bronze table, from CREATE up to the terminating semicolon. Strip -- line
    # comments first: WHY-comments carry semicolons that would otherwise end the statement match prematurely.
    raw = BRONZE_SQL.read_text(encoding="utf-8")
    ddl = "\n".join(re.sub(r"--.*$", "", line) for line in raw.splitlines())
    m = re.search(rf"CREATE TABLE IF NOT EXISTS bronze\.{table}\b(.*?);", ddl, re.S)
    assert m, f"could not locate bronze.{table} DDL"
    return m.group(0)


# --- P8a layer 2: ReplacingMergeTree dedup backstops ---------------------------------------------

def test_opensky_states_is_replacing_mergetree_with_fp_key():
    # The +977K offender must be a fingerprint-keyed RMT so replays collapse but recaptures survive; no version
    # column because committed_at is Nullable (illegal as one) and replay twins differ only in committed_at.
    sql = _table_ddl("opensky_states")
    assert "ENGINE = ReplacingMergeTree()" in sql, "states must be a no-version ReplacingMergeTree"
    assert re.search(r"ORDER BY\s*\(\s*snapshot_time\s*,\s*icao24\s*,\s*_dedup_fp\s*\)", sql), \
        "states ORDER BY must end in _dedup_fp so replays (identical fp) collapse"
    # Keep the sparse PK small (fp is dedup-only, not a prune key).
    assert re.search(r"PRIMARY KEY\s*\(\s*snapshot_time\s*,\s*icao24\s*\)", sql), \
        "states PRIMARY KEY must stay (snapshot_time, icao24)"


def test_dedup_fp_covers_source_columns_except_committed_at():
    # Excluding committed_at lets a replay (differs only there) collapse; dropping any other source col would
    # wrongly collapse two recaptures. Tie the tuple to STATES_COLUMNS so a schema change can't silently drift it.
    sql = _table_ddl("opensky_states")
    m = re.search(r"_dedup_fp\s+UInt64\s+MATERIALIZED\s+cityHash64\(toString\(tuple\((.*?)\)\)\)", sql, re.S)
    assert m, "_dedup_fp must be cityHash64(toString(tuple(<source cols>)))"
    fp_cols = [c.strip() for c in m.group(1).split(",") if c.strip()]
    assert fp_cols == [c for c in STATES_COLUMNS if c != "committed_at"], \
        "fp tuple must equal STATES_COLUMNS minus committed_at, in order"
    assert "committed_at" not in fp_cols


def test_adsb_states_stays_mergetree_rmt_deferred_to_p8b():
    # adsb has no dup today; its RMT backstop rides P8b's _raw_json-relocation rebuild instead of a wasted one here.
    sql = _table_ddl("adsb_states")
    assert "ReplacingMergeTree" not in sql, "adsb RMT backstop is deferred to P8b"
    assert re.search(r"ENGINE\s*=\s*MergeTree\b", sql), "adsb must be plain MergeTree in v6.1"


def test_flights_and_archive_stay_plain_mergetree():
    # flights source has a legit committed_at-distinct same-grain pair, so the states fp would collapse a real
    # row — flights (and the frozen, exact archive) must NOT be RMT-ified.
    for table in ("opensky_flights", "archive_states"):
        sql = _table_ddl(table)
        assert "ReplacingMergeTree" not in sql, f"{table} must stay plain MergeTree"
        assert "_dedup_fp" not in sql, f"{table} must not carry the states fingerprint"


def test_no_source_check_uses_final():
    # FINAL forces an unbounded merge-scan; the gate uses distinct content/grain over a closed window instead.
    for _name, ch_sql, ref_sql, _cmp in p.source_checks(1_782_100_000):
        assert "FINAL" not in ch_sql.upper()
        assert "FINAL" not in ref_sql.upper()
