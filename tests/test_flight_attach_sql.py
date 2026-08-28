from pathlib import Path


ROOT = Path(__file__).parents[1]
ATTACHED = ROOT / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_attached_votes.sql"
FINAL = ROOT / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_attach.sql"


def test_windowed_votes_are_materialized_before_vrs_scoring():
    # Re-inlining the votes as a CTE re-evaluates the candidate join for the VRS branch (42.8 GiB incident).
    final = FINAL.read_text()

    assert "ref('int_flight_attached_votes')" in final
    assert "ref('int_flight_opinions')" not in final


def test_attachment_join_is_bounded_by_an_overlap_day_key():
    # The day key is the actual fix: it cut the pre-predicate same-hex join from 484M to 7.7M pairs.
    attached = ATTACHED.read_text()

    assert attached.count("arrayJoin(range(") == 2
    assert "sp.overlap_day = o.overlap_day" in attached


def test_attachment_stages_keep_query_memory_backstops():
    # Per-model caps below the 16 GB profile default, so a regression reds one model instead of the host.
    assert "'max_memory_usage': 8000000000" in ATTACHED.read_text()
    assert "'max_memory_usage': 4000000000" in FINAL.read_text()
