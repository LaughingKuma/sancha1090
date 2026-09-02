from pathlib import Path

ROOT = Path(__file__).parents[1]
SPINE = ROOT / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_spine.sql"


def test_anti_joins_are_bounded_by_an_overlap_day_key():
    # icao24 alone paired every anchor with every same-hex record ever: 94.8M and 195.5M pre-predicate
    # pairs on the two anti-joins (measured 2026-09-01), the same curve int_flight_attach rode (#192).
    spine = SPINE.read_text()

    assert spine.count("overlap_days(") == 4
    assert "f.icao24 = a.icao24 and f.overlap_day = a.overlap_day" in spine
    assert "h.icao24 = a.icao24 and h.overlap_day = a.overlap_day" in spine


def test_spine_keeps_query_memory_backstop():
    # Below the 16 GB profile default so a regression reds this model instead of competing with the host.
    assert "'max_memory_usage': 4000000000" in SPINE.read_text()
