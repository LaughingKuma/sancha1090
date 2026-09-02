from pathlib import Path

ROOT = Path(__file__).parents[1]
RECONCILED = ROOT / "dbt" / "sancha1090" / "models" / "marts" / "fct_flights_reconciled.sql"


def _box_observed_cte() -> str:
    # Scope assertions to the CTE itself so a rewrite elsewhere can't satisfy them by accident.
    text = RECONCILED.read_text()
    start = text.index("box_observed as (")
    return text[start:text.index("\n),", start)]


def test_box_observed_join_is_bounded_by_a_day_key():
    # icao24 alone paired every spine row with every same-hex fix in history (2.6B pairs, 227 CPU-s/tick; #191).
    cte = _box_observed_cte()

    assert cte.count("overlap_days(") == 1
    assert "s.icao24 = sp.icao24 and s.overlap_day = sp.overlap_day" in cte
    assert "s.snapshot_time between sp.flight_start and sp.flight_end" in cte


def test_box_observed_prefilters_bronze_with_the_japan_box():
    # The box predicate must stay inside the bronze-side subquery, or the probe stream reverts to all-history.
    cte = _box_observed_cte()

    assert "source('bronze', 'opensky_states')" in cte
    assert "in_japan_box(" in cte


def test_box_observed_is_referenced_once():
    # ClickHouse inlines a CTE per reference; a second consumer would re-run the bronze scan.
    assert RECONCILED.read_text().count("from box_observed") == 1


def test_reconciled_keeps_query_memory_backstop():
    # Below the 16 GB profile default so a regression reds this model instead of competing with the host.
    assert "'max_memory_usage': 12000000000" in RECONCILED.read_text()
