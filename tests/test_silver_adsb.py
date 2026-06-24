from __future__ import annotations


# Doc sanity targets: these ICAO designators must resolve through the callsign->dim_airlines join.
TOP_AIRLINE_ANCHORS = {"ANA", "JAL", "CPA", "UAL", "EVA", "CAL"}


def _q(ch_cur, sql):
    ch_cur.execute(sql)
    return ch_cur.fetchall()


def test_fct_is_row_count_preserving(ch_cur):
    # ALL-LEFT joins + single-valued dims must neither drop nor fan out rows.
    fct = _q(ch_cur, "SELECT count(*) FROM silver_ch.fct_adsb_state")[0][0]
    bronze = _q(ch_cur, "SELECT count(*) FROM bronze.adsb_states")[0][0]
    assert fct == bronze, f"fct {fct} != bronze {bronze}"


def test_military_decode_matches_bronze(ch_cur):
    # Mirror the model's CH decode against the baked column: bitAnd(db_flags, 1) (v6.3 dropped _raw_json from CH).
    fct = _q(ch_cur, "SELECT count(DISTINCT hex) FROM silver_ch.fct_adsb_state WHERE is_military")[0][0]
    bronze = _q(ch_cur, "SELECT count(DISTINCT hex) FROM bronze.adsb_states "
                        "WHERE bitAnd(db_flags, 1) != 0")[0][0]
    assert fct == bronze and fct > 0, f"military hex fct={fct} bronze={bronze}"


def test_decode_booleans_are_two_valued(ch_cur):
    # coalesce(db_flags,0): exception flags are TRUE/FALSE, never NULL, so filters, GROUP BY and
    # avg(% military) all behave downstream instead of silently dropping 97% of rows.
    nulls = _q(ch_cur, "SELECT count(*) FROM silver_ch.fct_adsb_state "
                       "WHERE is_military IS NULL OR is_ladd IS NULL OR is_pia IS NULL OR is_interesting IS NULL")[0][0]
    assert nulls == 0, f"{nulls} rows have a NULL decode boolean"


def test_dim_aircraft_join_hit_rate_is_high(ch_cur):
    # bronze r/t/desc cover ~99% of the observed fleet, so registration should be present for most rows.
    pct = _q(ch_cur, "SELECT 100.0*count(registration)/count(*) FROM silver_ch.fct_adsb_state")[0][0]
    assert pct >= 95, f"dim_aircraft registration hit-rate too low: {pct}%"


def test_ga_callsign_never_matches_airline(ch_cur):
    # The regex guard must keep GA/registration tails (e.g. JA45KA) from false-matching an airline.
    # Keyed on callsign_filled — the model's actual airline join key (post-v5.12 backfill).
    leaked = _q(ch_cur, "SELECT count(*) FROM silver_ch.fct_adsb_state "
                        "WHERE airline_name IS NOT NULL AND NOT match(trimBoth(callsign_filled),'^[A-Z]{3}[0-9]')")[0][0]
    assert leaked == 0, f"{leaked} GA callsigns matched an airline"


def test_top_airlines_match_doc_targets(ch_cur):
    # callsign_filled (the model's airline join key) not raw flight: else v5.12 backfilled blank-flight airframes clump under a NULL prefix and outrank ANA.
    rows = _q(ch_cur, "SELECT substr(trimBoth(callsign_filled),1,3) d, count(DISTINCT hex) n FROM silver_ch.fct_adsb_state "
                      "WHERE airline_name IS NOT NULL GROUP BY d ORDER BY n DESC LIMIT 12")
    top = [r[0] for r in rows]
    assert top[0] == "ANA", f"expected ANA as #1 airline, got {top[:3]}"
    assert TOP_AIRLINE_ANCHORS <= set(top), f"missing doc anchors: {TOP_AIRLINE_ANCHORS - set(top)}"


def test_country_resolves_for_japan_hex(ch_cur):
    # Deterministic from hex via the range_hashed dict; 84xxxx is Japan.
    rows = _q(ch_cur, "SELECT DISTINCT reg_country FROM silver_ch.fct_adsb_state "
                      "WHERE substr(lower(hex),1,2) IN ('84','85','86','87')")
    countries = sorted(r[0] for r in rows)
    assert countries == ["Japan"], countries
