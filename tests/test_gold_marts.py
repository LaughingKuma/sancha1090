from __future__ import annotations


# Keep in sync with var legs_cruise_alt_m in dbt_project.yml.
CRUISE_ALT_M = 3000


def _q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def test_dim_airports_anchors_present_and_unique(cur):
    rows = _q(cur, "SELECT icao FROM silver.dim_airports WHERE icao IN ('RJTT','RJAA','KJFK','EGLL')")
    assert {r[0] for r in rows} == {"RJTT", "RJAA", "KJFK", "EGLL"}
    dupes = _q(cur, "SELECT count(*) - count(DISTINCT icao) FROM silver.dim_airports")[0][0]
    assert dupes == 0, f"{dupes} duplicate ICAO rows in dim_airports"


def test_flight_legs_no_fanout(cur):
    total, distinct = _q(cur, "SELECT count(*), count(DISTINCT (icao24, leg_id)) "
                              "FROM gold.fct_flight_legs")[0]
    assert total == distinct, f"fan-out: {total} rows vs {distinct} distinct (icao24, leg_id)"


def test_flight_legs_time_ordered(cur):
    bad = _q(cur, "SELECT count(*) FROM gold.fct_flight_legs WHERE start_time > end_time")[0][0]
    assert bad == 0, f"{bad} legs with start_time > end_time"


def test_no_endpoint_snapped_above_cruise(cur):
    bad = _q(cur, "SELECT count(*) FROM gold.fct_flight_legs "
                  f"WHERE (origin_icao IS NOT NULL AND first_alt_m >= {CRUISE_ALT_M}) "
                  f"   OR (dest_icao   IS NOT NULL AND last_alt_m  >= {CRUISE_ALT_M})")[0][0]
    assert bad == 0, f"{bad} legs snapped an airport to a cruise-altitude fix (overflight)"


def test_route_inferred_consistent_with_endpoints(cur):
    bad = _q(cur, "SELECT count(*) FROM gold.fct_flight_legs "
                  "WHERE (route_inferred IS NOT NULL AND (origin_icao IS NULL OR dest_icao IS NULL)) "
                  "   OR (route_inferred IS NULL AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL)")[0][0]
    assert bad == 0, f"{bad} legs with route_inferred inconsistent with origin/dest"


def test_tokyo_airports_appear_as_endpoints(cur):
    n = _q(cur, "SELECT count(*) FROM gold.fct_flight_legs "
                "WHERE origin_icao IN ('RJTT','RJAA') OR dest_icao IN ('RJTT','RJAA')")[0][0]
    assert n > 0, "no legs touch Tokyo airports RJTT/RJAA"


def test_crossed_antenna_is_subset_of_fleet(cur):
    crossed = _q(cur, "SELECT count(DISTINCT icao24) FROM gold.fct_flight_legs WHERE crossed_antenna")[0][0]
    fleet = _q(cur, "SELECT count(DISTINCT hex) FROM silver.fct_adsb_state")[0][0]
    assert 0 < crossed <= fleet, f"crossed_antenna airframes {crossed} not in (0, fleet={fleet}]"


def test_flight_legs_airline_join_resolves_without_ga_leak(cur):
    # The leg airline join keys on the leg's OWN callsign — a distinct path from agg_airline_traffic.
    with_airline, ga_leak = _q(cur, "SELECT count(airline_name), "
        "count_if(airline_name IS NOT NULL AND NOT regexp_like(trim(callsign), '^[A-Z]{3}[0-9]')) "
        "FROM gold.fct_flight_legs")[0]
    assert with_airline > 0, "leg airline join resolved zero airlines"
    assert ga_leak == 0, f"{ga_leak} legs matched an airline despite failing the GA-tail guard"


def test_flight_legs_duration_and_fixes_nonnegative(cur):
    neg_dur, bad_fixes = _q(cur, "SELECT count_if(duration_min < 0), count_if(num_fixes <= 0) "
                                 "FROM gold.fct_flight_legs")[0]
    assert neg_dur == 0, f"{neg_dur} legs with negative duration_min"
    assert bad_fixes == 0, f"{bad_fixes} legs with num_fixes <= 0"


def test_agg_route_traffic_top_route_valid(cur):
    rows = _q(cur, "SELECT route_inferred, origin_icao, dest_icao, leg_count, origin_lat, dest_lon "
                   "FROM gold.agg_route_traffic ORDER BY leg_count DESC LIMIT 1")
    assert rows, "agg_route_traffic is empty"
    route, origin, dest, leg_count, olat, dlon = rows[0]
    assert leg_count > 0
    assert origin != dest, f"degenerate self-loop route {route}"
    assert olat is not None and dlon is not None, "arc coords must be non-null for the map"


def test_agg_airline_traffic_resolves_anchor_airlines(cur):
    names = {r[0] for r in _q(cur, "SELECT DISTINCT airline_name FROM gold.agg_airline_traffic")}
    # Contract (drift-proof): always populated, never a null/empty airline name.
    assert names, "agg_airline_traffic produced no airlines"
    assert all(n and n.strip() for n in names), "null/empty airline_name leaked into the agg"
    # Anchors (deliberate live regression targets, cf. test_silver_adsb.py): the OpenSky context feed always
    # carries ANA + hundreds of carriers, so these are stable, not brittle.
    assert len(names) >= 20, f"too few airlines resolved: {len(names)}"
    assert any("Nippon" in (n or "") for n in names), "expected an ANA/Nippon airline in the OpenSky context feed"


def test_agg_airline_traffic_is_hourly(cur):
    n = _q(cur, "SELECT count(DISTINCT snapshot_hour) FROM gold.agg_airline_traffic")[0][0]
    assert n > 0, "no hourly buckets"


def test_agg_country_traffic_adsb_japan_top(cur):
    rows = _q(cur, "SELECT reg_country, distinct_aircraft FROM gold.agg_country_traffic_adsb "
                   "WHERE reg_country IS NOT NULL ORDER BY distinct_aircraft DESC LIMIT 3")
    # Contract (drift-proof): a non-null top country with a positive aircraft count.
    assert rows, "agg_country_traffic_adsb is empty"
    country, n = rows[0]
    assert country and n > 0, f"invalid top row: {rows[0]}"
    # Anchor: the antenna is fixed in Tokyo, so Japan is always among the top reg_countries — but the
    # Japan+ocean box also catches heavy transpacific US/China traffic, so a near-tie can edge Japan off
    # strict #1 (seen US 684 / Japan 681). Assert top-3 membership rather than rank-1 to stay drift-proof,
    # but require Japan to track the leader closely — a real regression (geo-filter/feed loss) would sink it.
    counts = {c: cnt for c, cnt in rows}
    assert "Japan" in counts, f"expected Japan among top-3 reg_countries, got {list(counts)}"
    leader_n = rows[0][1]
    assert counts["Japan"] >= 0.7 * leader_n, f"Japan far below leader: {counts['Japan']} vs {leader_n}"
