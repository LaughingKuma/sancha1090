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


def test_landed_never_refetches():
    eng = _engine()
    ledger.record_attempts([("a61c53", "2026-06-25", "landed")], eng)
    assert ledger.filter_unattempted([("a61c53", "2026-06-25")], eng) == []


def test_missing_retries_once_after_cooldown_then_permanent():
    eng = _engine()
    ledger.record_attempts([("a61c53", "2026-06-25", "missing")], eng)
    # Fresh miss: inside the cooldown, not retried.
    assert ledger.filter_unattempted([("a61c53", "2026-06-25")], eng) == []
    # Age the attempt past the cooldown by rewriting attempted_at.
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    assert ledger.filter_unattempted([("a61c53", "2026-06-25")], eng) == [("a61c53", "2026-06-25")]
    # Second miss -> attempts=2 == max_attempts -> permanent skip even when aged.
    ledger.record_attempts([("a61c53", "2026-06-25", "missing")], eng)
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    assert ledger.filter_unattempted([("a61c53", "2026-06-25")], eng) == []


def test_error_retries_after_short_cooldown_without_attempt_cap():
    eng = _engine()
    pair = ("a61c53", "2026-06-25")
    for _ in range(3):
        ledger.record_attempts([(*pair, "error")], eng)
    # Fresh errors wait long enough for Airflow's retry delay, avoiding an immediate hot loop.
    assert ledger.filter_unattempted([pair], eng) == []
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    assert ledger.filter_unattempted([pair], eng) == [pair]


def test_due_error_pairs_returns_only_aged_errors_in_stable_order():
    eng = _engine()
    ledger.record_attempts([
        ("bbbbbb", "2026-06-24", "error"),
        ("aaaaaa", "2026-06-24", "error"),
        ("cccccc", "2026-06-24", "missing"),
        ("dddddd", "2026-06-24", "landed"),
    ], eng)
    assert ledger.due_error_pairs(eng) == []
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    assert ledger.due_error_pairs(eng) == [
        ("aaaaaa", "2026-06-24"),
        ("bbbbbb", "2026-06-24"),
    ]
    assert ledger.due_error_pairs(eng, limit=1) == [("aaaaaa", "2026-06-24")]
    assert ledger.due_error_pairs(eng, limit=0) == []


def test_due_missing_pairs_returns_only_aged_missing_under_cap_in_stable_order():
    eng = _engine()
    ledger.record_attempts([
        ("bbbbbb", "2026-06-24", "missing"),
        ("aaaaaa", "2026-06-24", "missing"),
        ("cccccc", "2026-06-24", "error"),
        ("dddddd", "2026-06-24", "landed"),
    ], eng)
    # Fresh misses: inside the 7-day aging window, not due yet.
    assert ledger.due_missing_pairs(eng) == []
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    # Aged missing pairs are due; error/landed outcomes are excluded regardless of age.
    assert ledger.due_missing_pairs(eng) == [
        ("aaaaaa", "2026-06-24"),
        ("bbbbbb", "2026-06-24"),
    ]
    assert ledger.due_missing_pairs(eng, limit=1) == [("aaaaaa", "2026-06-24")]
    assert ledger.due_missing_pairs(eng, limit=0) == []


def test_due_missing_pairs_excludes_attempts_at_cap():
    eng = _engine()
    ledger.record_attempts([("aaaaaa", "2026-06-24", "missing")], eng)
    ledger.record_attempts([("aaaaaa", "2026-06-24", "missing")], eng)  # attempts=2 == max_attempts
    with eng.begin() as conn:
        conn.execute(sa.text(
            "UPDATE adsblol_route_attempts SET attempted_at = '2020-01-01 00:00:00+00:00'"))
    assert ledger.due_missing_pairs(eng) == []


def test_delete_attempts_reenables_refetch():
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
