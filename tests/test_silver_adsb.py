from __future__ import annotations

import os

import pytest


# Doc sanity targets: these ICAO designators must resolve through the callsign->dim_airlines join.
TOP_AIRLINE_ANCHORS = {"ANA", "JAL", "CPA", "UAL", "EVA", "CAL"}


@pytest.fixture(scope="module")
def cur():
    try:
        import trino
    except ImportError as exc:
        pytest.skip(f"trino client not available: {exc}")
    try:
        conn = trino.dbapi.connect(
            host=os.environ.get("TRINO_HOST", "trino-coordinator"),
            port=int(os.environ.get("TRINO_PORT", "8080")),
            user="root", catalog="iceberg", http_scheme="http",
        )
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchall()
    except Exception as exc:  # only infra unreachability skips; missing tables must fail loudly (RED)
        pytest.skip(f"trino not reachable: {exc}")
    return c


def _q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def test_fct_is_row_count_preserving(cur):
    # ALL-LEFT joins + single-valued dims must neither drop nor fan out rows.
    fct = _q(cur, "SELECT count(*) FROM silver.fct_adsb_state")[0][0]
    bronze = _q(cur, "SELECT count(*) FROM bronze.adsb_states")[0][0]
    assert fct == bronze, f"fct {fct} != bronze {bronze}"


def test_military_decode_matches_bronze(cur):
    fct = _q(cur, "SELECT count(DISTINCT hex) FROM silver.fct_adsb_state WHERE is_military")[0][0]
    bronze = _q(cur, "SELECT count(DISTINCT hex) FROM bronze.adsb_states "
                     "WHERE bitwise_and(TRY_CAST(json_extract_scalar(_raw_json,'$.dbFlags') AS INTEGER),1)<>0")[0][0]
    assert fct == bronze and fct > 0, f"military hex fct={fct} bronze={bronze}"


def test_decode_booleans_are_two_valued(cur):
    # COALESCE(db_flags,0): exception flags are TRUE/FALSE, never NULL, so `= false`, GROUP BY and
    # avg(cast(... as int)) (% military) all behave downstream instead of silently dropping 97% of rows.
    nulls = _q(cur, "SELECT count(*) FROM silver.fct_adsb_state "
                    "WHERE is_military IS NULL OR is_ladd IS NULL OR is_pia IS NULL OR is_interesting IS NULL")[0][0]
    assert nulls == 0, f"{nulls} rows have a NULL decode boolean"


def test_dim_aircraft_join_hit_rate_is_high(cur):
    # bronze r/t/desc cover ~99% of the observed fleet, so registration should be present for most rows.
    pct = _q(cur, "SELECT 100.0*count(registration)/count(*) FROM silver.fct_adsb_state")[0][0]
    assert pct >= 95, f"dim_aircraft registration hit-rate too low: {pct}%"


def test_ga_callsign_never_matches_airline(cur):
    # The regex guard must keep GA/registration tails (e.g. JA45KA) from false-matching an airline.
    leaked = _q(cur, "SELECT count(*) FROM silver.fct_adsb_state "
                     "WHERE airline_name IS NOT NULL AND NOT regexp_like(trim(flight),'^[A-Z]{3}[0-9]')")[0][0]
    assert leaked == 0, f"{leaked} GA callsigns matched an airline"


def test_top_airlines_match_doc_targets(cur):
    rows = _q(cur, "SELECT substr(trim(flight),1,3) d, count(DISTINCT hex) n FROM silver.fct_adsb_state "
                   "WHERE airline_name IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 12")
    top = [r[0] for r in rows]
    assert top[0] == "ANA", f"expected ANA as #1 airline, got {top[:3]}"
    assert TOP_AIRLINE_ANCHORS <= set(top), f"missing doc anchors: {TOP_AIRLINE_ANCHORS - set(top)}"


def test_country_resolves_for_japan_hex(cur):
    # Deterministic from hex via the disjoint range table; 84xxxx is Japan.
    rows = _q(cur, "SELECT DISTINCT reg_country FROM silver.fct_adsb_state "
                   "WHERE substr(lower(hex),1,2) IN ('84','85','86','87')")
    assert rows == [["Japan"]], rows
