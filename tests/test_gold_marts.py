from __future__ import annotations


# Keep in sync with var legs_cruise_alt_m in dbt_project.yml.
CRUISE_ALT_M = 3000


def _q(ch_cur, sql):
    ch_cur.execute(sql)
    return ch_cur.fetchall()


def test_dim_airports_anchors_present_and_unique(ch_cur):
    rows = _q(ch_cur, "SELECT icao FROM silver_ch.dim_airports WHERE icao IN ('RJTT','RJAA','KJFK','EGLL')")
    assert {r[0] for r in rows} == {"RJTT", "RJAA", "KJFK", "EGLL"}
    dupes = _q(ch_cur, "SELECT count(*) - count(DISTINCT icao) FROM silver_ch.dim_airports")[0][0]
    assert dupes == 0, f"{dupes} duplicate ICAO rows in dim_airports"


def test_flight_legs_no_fanout(ch_cur):
    total, distinct = _q(ch_cur, "SELECT count(*), count(DISTINCT icao24, leg_id) "
                                 "FROM gold_ch.fct_flight_legs")[0]
    assert total == distinct, f"fan-out: {total} rows vs {distinct} distinct (icao24, leg_id)"


def test_flight_legs_time_ordered(ch_cur):
    bad = _q(ch_cur, "SELECT count(*) FROM gold_ch.fct_flight_legs WHERE start_time > end_time")[0][0]
    assert bad == 0, f"{bad} legs with start_time > end_time"


def test_no_endpoint_snapped_above_cruise(ch_cur):
    # Scoped per endpoint (v6.10): a chained/curated endpoint on the same leg legitimately
    # sits at cruise; only the geometric snap is altitude-bound.
    bad = _q(ch_cur, "SELECT count(*) FROM gold_ch.fct_flight_legs "
                     f"WHERE (origin_source = 'snap' AND origin_icao IS NOT NULL AND first_alt_m >= {CRUISE_ALT_M}) "
                     f"   OR (dest_source   = 'snap' AND dest_icao   IS NOT NULL AND last_alt_m  >= {CRUISE_ALT_M})")[0][0]
    assert bad == 0, f"{bad} snap-attributed endpoints with a cruise-altitude fix"


def test_route_inferred_consistent_with_endpoints(ch_cur):
    bad = _q(ch_cur, "SELECT count(*) FROM gold_ch.fct_flight_legs "
                     "WHERE (route_inferred IS NOT NULL AND (origin_icao IS NULL OR dest_icao IS NULL)) "
                     "   OR (route_inferred IS NULL AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL)")[0][0]
    assert bad == 0, f"{bad} legs with route_inferred inconsistent with origin/dest"


def test_tokyo_airports_appear_as_endpoints(ch_cur):
    n = _q(ch_cur, "SELECT count(*) FROM gold_ch.fct_flight_legs "
                   "WHERE origin_icao IN ('RJTT','RJAA') OR dest_icao IN ('RJTT','RJAA')")[0][0]
    assert n > 0, "no legs touch Tokyo airports RJTT/RJAA"


def test_crossed_antenna_is_subset_of_fleet(ch_cur):
    crossed = _q(ch_cur, "SELECT count(DISTINCT icao24) FROM gold_ch.fct_flight_legs WHERE crossed_antenna")[0][0]
    fleet = _q(ch_cur, "SELECT count(DISTINCT hex) FROM silver_ch.fct_adsb_state")[0][0]
    assert 0 < crossed <= fleet, f"crossed_antenna airframes {crossed} not in (0, fleet={fleet}]"


def test_flight_legs_airline_join_resolves_without_ga_leak(ch_cur):
    # The leg airline join keys on the leg's OWN callsign — a distinct path from agg_airline_traffic.
    with_airline, ga_leak = _q(ch_cur, "SELECT count(airline_name), "
        "countIf(airline_name IS NOT NULL AND NOT match(trimBoth(callsign), '^[A-Z]{3}[0-9]')) "
        "FROM gold_ch.fct_flight_legs")[0]
    assert with_airline > 0, "leg airline join resolved zero airlines"
    assert ga_leak == 0, f"{ga_leak} legs matched an airline despite failing the GA-tail guard"


def test_flight_legs_duration_and_fixes_nonnegative(ch_cur):
    neg_dur, bad_fixes = _q(ch_cur, "SELECT countIf(duration_min < 0), countIf(num_fixes <= 0) "
                                    "FROM gold_ch.fct_flight_legs")[0]
    assert neg_dur == 0, f"{neg_dur} legs with negative duration_min"
    assert bad_fixes == 0, f"{bad_fixes} legs with num_fixes <= 0"


def test_agg_route_traffic_top_route_valid(ch_cur):
    rows = _q(ch_cur, "SELECT route_inferred, origin_icao, dest_icao, flight_count, origin_lat, dest_lon "
                      "FROM gold_ch.agg_route_traffic ORDER BY flight_count DESC LIMIT 1")
    assert rows, "agg_route_traffic is empty"
    route, origin, dest, flight_count, olat, dlon = rows[0]
    assert flight_count > 0
    assert origin != dest, f"degenerate self-loop route {route}"
    assert olat is not None and dlon is not None, "arc coords must be non-null for the map"


def test_agg_airline_traffic_resolves_anchor_airlines(ch_cur):
    names = {r[0] for r in _q(ch_cur, "SELECT DISTINCT airline_name FROM gold_ch.agg_airline_traffic")}
    # Contract (drift-proof): always populated, never a null/empty airline name.
    assert names, "agg_airline_traffic produced no airlines"
    assert all(n and n.strip() for n in names), "null/empty airline_name leaked into the agg"
    # Anchors (deliberate live regression targets, cf. test_silver_adsb.py): the OpenSky context feed always
    # carries ANA + hundreds of carriers, so these are stable, not brittle.
    assert len(names) >= 20, f"too few airlines resolved: {len(names)}"
    assert any("Nippon" in (n or "") for n in names), "expected an ANA/Nippon airline in the OpenSky context feed"


def test_agg_airline_traffic_is_hourly(ch_cur):
    n = _q(ch_cur, "SELECT count(DISTINCT snapshot_hour) FROM gold_ch.agg_airline_traffic")[0][0]
    assert n > 0, "no hourly buckets"


def test_agg_country_traffic_adsb_japan_top(ch_cur):
    rows = _q(ch_cur, "SELECT reg_country, distinct_aircraft FROM gold_ch.agg_country_traffic_adsb "
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


def test_airline_legs_never_snap_to_unscheduled_airports(ch_cur):
    # Mirror of the dbt singular guard: the sched gate is a hard candidate filter.
    bad = _q(ch_cur, "SELECT count(*) FROM gold_ch.fct_flight_legs l "
                     "LEFT JOIN silver_ch.dim_airports oa ON oa.icao = l.origin_icao "
                     "LEFT JOIN silver_ch.dim_airports da ON da.icao = l.dest_icao "
                     "WHERE l.callsign IS NOT NULL AND match(trimBoth(l.callsign), '^[A-Z]{3}[0-9]') "
                     "AND ((l.origin_source = 'snap' AND oa.icao IS NOT NULL AND NOT oa.scheduled_service) "
                     "  OR (l.dest_source = 'snap' AND da.icao IS NOT NULL AND NOT da.scheduled_service))")[0][0]
    assert bad == 0, f"{bad} airline-shaped legs snapped to unscheduled airports"
