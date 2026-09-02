from pathlib import Path

from conftest import flat_sql

ROOT = Path(__file__).parents[1]
MODELS = ROOT / "dbt" / "sancha1090" / "models"
SILVER = MODELS / "silver"
LATEST = SILVER / "int_swim_latest.sql"
FLIGHT = SILVER / "int_swim_flight.sql"
MV_MODULE = ROOT / "include" / "ch_incremental_mvs.py"


def test_latest_amendment_is_materialized_before_hex_resolution():
    # Re-inlining `latest` as a CTE re-runs the 77M-row GROUP BY once per reference (~5x per tick, #187).
    flight = FLIGHT.read_text()

    assert flight.count("ref('int_swim_latest')") == 4  # bounds, swim_callsigns, scored, final select
    assert "source('bronze', 'swim_flightdata')" not in flight
    assert "argMax(" not in flight


def test_latest_reads_the_mv_and_owns_lid_resolution():
    # #201: the amendment aggregation moved to the P4 MV; dbt reads its -Merge view and must keep NO copy of
    # the bronze scan, the argMax, or the flight_key expression (a drifted copy is silent O/D corruption).
    latest = LATEST.read_text()

    assert "source('silver_ch', 'swim_latest')" in latest
    assert "source('bronze', 'swim_flightdata')" not in latest
    assert "argMax(" not in latest
    assert "coalesce(gufi" not in latest
    assert "ref('dim_airports')" in latest
    assert "group by iata" in latest  # the fan-out guard; its unique test in _swim.yml is the runtime tripwire


def test_flight_key_expression_has_exactly_one_home():
    # Single source (#201): the identity expression lives in the MV spec only. A hand-synced dbt copy would
    # drift silently — the two would key different rows and the mart would serve the wrong amendment.
    from include.ch_incremental_mvs import _SWIM_FLIGHT_KEY

    key = flat_sql(_SWIM_FLIGHT_KEY, squash=True)
    assert flat_sql(MV_MODULE.read_text(), squash=True).count(key) == 1, \
        "flight_key expression is not defined exactly once"
    # Every model dir, not just silver/: a copy is just as corrupting from gold/ or a future subdir, and the
    # squashed compare means neither reformatting nor re-casing can hide one.
    for sql in sorted(MODELS.rglob("*.sql")):
        assert key not in flat_sql(sql.read_text(), squash=True), \
            f"{sql.relative_to(MODELS)} carries a drift copy of the flight_key expression"


def test_swim_stages_keep_query_memory_backstops():
    # Caps bound each INSERT (dbt reds the model at the cap); host protection is the 24 GB server budget + cgroup.
    # 7e9 puts the headroom alarm line (0.8x) at 5.6 GB, above the measured 4.97 GB peak (#208).
    assert "'max_memory_usage': 7000000000" in LATEST.read_text()
    assert "'max_memory_usage': 4000000000" in FLIGHT.read_text()
