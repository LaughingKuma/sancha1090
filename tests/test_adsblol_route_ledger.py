from __future__ import annotations

import sqlalchemy as sa

import include.adsblol_route_ledger as ledger


def _engine():
    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    return eng


def test_unseen_pairs_pass_through():
    eng = _engine()
    pairs = [("a61c53", "2026-06-25"), ("a57362", "2026-06-25")]
    assert ledger.filter_unattempted(pairs, eng) == pairs


def test_landed_never_reextracts():
    eng = _engine()
    ledger.record_attempts([("a61c53", "2026-06-25", "landed")], eng)
    assert ledger.filter_unattempted([("a61c53", "2026-06-25")], eng) == []


def test_missing_and_error_are_reproposed():
    # A tar is streamed once per trace day, so absence is final only for that stream: a later
    # re-land (a different variant, a repair trigger) must still see these pairs.
    eng = _engine()
    ledger.record_attempts([("a61c53", "2026-06-25", "missing"),
                            ("ffff01", "2026-06-25", "error")], eng)
    pairs = [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25")]
    assert ledger.filter_unattempted(pairs, eng) == pairs


def test_delete_attempts_reenables_reextract():
    eng = _engine()
    ledger.record_attempts([("a61c53", "2026-06-25", "landed"),
                            ("ffff01", "2026-06-25", "landed"),
                            ("a61c53", "2026-06-26", "landed")], eng)
    # Clear only the two 2026-06-25 pairs; the 2026-06-26 row must survive.
    n = ledger.delete_attempts([("a61c53", "2026-06-25"), ("ffff01", "2026-06-25")], eng)
    assert n == 2
    assert ledger.filter_unattempted(
        [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25")], eng) == \
        [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25")]
    assert ledger.filter_unattempted([("a61c53", "2026-06-26")], eng) == []


def test_delete_attempts_empty_is_noop():
    eng = _engine()
    assert ledger.delete_attempts([], eng) == 0


def test_record_attempts_upserts_and_counts():
    eng = _engine()
    assert ledger.record_attempts([("a61c53", "2026-06-25", "missing")], eng) == 1
    assert ledger.record_attempts([("a61c53", "2026-06-25", "landed")], eng) == 1
    with eng.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT outcome, attempts FROM adsblol_route_attempts")).one()
    assert row.outcome == "landed" and row.attempts == 2


def test_release_landing_reads_back_the_recorded_row():
    eng = _engine()
    assert ledger.release_landing("2026-08-20", eng) is None
    ledger.record_release_landing(
        "2026-08-20", repo="globe_history_2026", tag="v2026.08.20-planes-readsb-prod-0",
        parts=3, members=61234, targets=3557, landed=3400, missing=150, errors=7, engine=eng)
    row = ledger.release_landing("2026-08-20", eng)
    assert row["repo"] == "globe_history_2026"
    assert row["tag"] == "v2026.08.20-planes-readsb-prod-0"
    assert (row["parts"], row["members"], row["targets"]) == (3, 61234, 3557)
    assert (row["landed"], row["missing"], row["errors"]) == (3400, 150, 7)
    assert row["landed_at"]


def test_record_release_landing_upserts_one_row_per_day():
    eng = _engine()
    for landed in (10, 20):
        ledger.record_release_landing(
            "2026-08-20", repo="globe_history_2026", tag="v2026.08.20-planes-readsb-staging-0",
            parts=1, members=5, targets=6, landed=landed, missing=0, errors=0, engine=eng)
    with eng.begin() as conn:
        rows = conn.execute(sa.text("SELECT landed FROM adsblol_release_landings")).all()
    assert [r.landed for r in rows] == [20]
