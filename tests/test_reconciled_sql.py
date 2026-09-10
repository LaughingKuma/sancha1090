from pathlib import Path

ROOT = Path(__file__).parents[1]
RECONCILED = ROOT / "dbt" / "sancha1090" / "models" / "marts" / "fct_flights_reconciled.sql"


BOX_LANES = ("opensky_states", "adsblol_states", "adsb_states")


def _box_cte_region() -> str:
    # Scope assertions to the box CTEs themselves so a rewrite elsewhere can't satisfy them by accident.
    text = RECONCILED.read_text()
    start = text.index("box_spine as (")
    return text[start:text.index("curated as (", start)]


def _lane_list() -> str:
    region = _box_cte_region()
    start = region.index("set box_lanes = [")
    return region[start:region.index("] %}", start)]


def _probe_body() -> str:
    # The one Jinja-looped arm every lane renders from.
    region = _box_cte_region()
    start = region.index("{%- for lane in box_lanes %}")
    return region[start:region.index("{%- endfor %}", start)]


def test_box_observed_join_is_bounded_by_a_day_key():
    # icao24 alone paired every spine row with every same-hex fix in history (2.6B pairs, 227 CPU-s/tick; #191).
    body = _probe_body()

    assert _box_cte_region().count("overlap_days(") == 1
    assert body.count("sp.icao24 = s.icao24 and sp.overlap_day = s.overlap_day") == 1
    assert body.count("s.snapshot_time between sp.flight_start and sp.flight_end") == 1
    assert body.count("union distinct") == 1


def test_box_observed_prefilters_bronze_with_the_japan_box():
    # The box predicate must stay inside the bronze-side subquery, or every probe stream reverts to all-history.
    lanes = _lane_list()
    body = _probe_body()

    for lane in BOX_LANES:
        assert f"'src': '{lane}'" in lanes
    assert lanes.count("'src':") == len(BOX_LANES)
    assert "source('bronze', lane.src)" in body
    assert body.count("in_japan_box(lane.lat, lane.lon)") == 1


def test_box_observed_extra_lanes_probe_only_adsblol_anchors():
    # The gate drops only adsblol anchors (#213 D); probing every spine row would triple the join for nothing.
    lanes = _lane_list()

    assert lanes.count("'anchor_filter': \"anchor_source = 'adsblol'\"") == len(BOX_LANES) - 1
    opensky_lane = lanes[lanes.index("'src': 'opensky_states'"):lanes.index("'src': 'adsblol_states'")]
    assert "'anchor_filter': none" in opensky_lane
    assert "where {{ lane.anchor_filter }}" in _probe_body()


def test_box_spine_skips_opensky_flights_anchors():
    # resolved admits opensky_flights anchors without the gate, so hashing them only widens every lane's probe.
    assert "anchor_source != 'opensky_flights'" in _box_cte_region()


def test_box_observed_is_referenced_once():
    # ClickHouse inlines a CTE per reference; a second consumer would re-run the bronze scan.
    assert RECONCILED.read_text().count("from box_observed") == 1


def test_reconciled_keeps_query_memory_backstop():
    # Below the 16 GB profile default so a regression reds this model instead of competing with the host.
    assert "'max_memory_usage': 12000000000" in RECONCILED.read_text()


def _trace_end_cte() -> str:
    text = RECONCILED.read_text()
    start = text.index("trace_end as (")
    return text[start:text.index("resolved as (", start)]


def test_trace_end_reads_the_chain_that_voted():
    # #214: the label follows the vote -- the chain int_flight_attached_votes attached, keyed (icao24, win_start), never
    # a chain re-picked from a window overlap (an over-cap chain cannot vote; a spanning chain attaches to one flight).
    cte = _trace_end_cte()

    assert "from {{ ref('int_flight_attached_votes') }} av" in cte
    assert "join {{ ref('int_flight_chains_adsblol') }} c on c.icao24 = av.icao24 and c.chain_start = av.win_start" in cte
    assert "where av.source = 'adsblol'" in cte
    assert "overlap_days(" not in cte
    assert "box_spine" not in cte
    text = RECONCILED.read_text()
    assert text.count("join trace_end te") == 1
    # a join column named flight_id makes `r.*` emit `r.flight_id`, which fct_flight_path's spine cannot read.
    assert "left join trace_end te on te.te_flight_id = r.flight_id" in text
    assert "select av.flight_id as te_flight_id," in cte


def test_coverage_reason_is_one_sided_and_cruise_gated():
    # 'coverage' means the trace ended airborne at the snap altitude gate on the missing side, never on a two-sided row.
    text = RECONCILED.read_text()
    assert "multiIf((r.origin_icao is null) = (r.dest_icao is null), null," in text
    assert text.count("{{ var('legs_cruise_alt_m') }}, 'coverage'") == 2
    assert "not te.first_on_ground and te.first_alt_m >=" in text
    assert "not te.last_on_ground and te.last_alt_m >=" in text
