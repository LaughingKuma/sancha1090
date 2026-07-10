from __future__ import annotations

import pytest

from include.ingest_summary import all_fetches_raised, raise_if_all_fetches_raised


def test_normal_run_with_data_is_not_a_failure():
    # One region attempted, one landed data -> healthy, no raise.
    assert all_fetches_raised(attempted=1, succeeded=1) is False


def test_partial_failure_is_tolerated():
    # Some fetches raised but at least one succeeded -> not a wholesale failure (a
    # single-region blip must not red the whole ingest).
    assert all_fetches_raised(attempted=3, succeeded=1) is False


def test_every_fetch_failed_is_a_wholesale_failure():
    # The 2026-06-30 incident: every fetch_region raised (manifest DB unreachable),
    # nothing succeeded -> the DAG must NOT report green.
    assert all_fetches_raised(attempted=3, succeeded=0) is True


def test_no_regions_attempted_is_not_a_failure():
    # Nothing to do (empty mapping) is not an ingest failure.
    assert all_fetches_raised(attempted=0, succeeded=0) is False


def test_all_succeeded_but_empty_is_not_a_failure():
    # Issue #103 finding 1: every fetch succeeded but legitimately returned no data
    # (e.g. the single japan region) -> a designed success path, must NOT red the run.
    assert all_fetches_raised(attempted=1, succeeded=1) is False


def test_mix_of_raised_and_empty_success_is_tolerated():
    # Some fetch tasks raised, the rest succeeded but empty -> still partial success,
    # not a wholesale outage.
    assert all_fetches_raised(attempted=3, succeeded=2) is False


def test_raise_helper_raises_on_wholesale_failure():
    summary = {"regions_attempted": 2, "regions_succeeded": 0}
    with pytest.raises(RuntimeError, match="all 2"):
        raise_if_all_fetches_raised(summary, entity="regions", label="ingest_states")


def test_raise_helper_silent_on_healthy_summary():
    summary = {"airports_attempted": 5, "airports_succeeded": 3}
    raise_if_all_fetches_raised(summary, entity="airports", label="ingest_flights")


def test_raise_helper_silent_when_all_succeeded_but_empty():
    # Same case as test_all_succeeded_but_empty_is_not_a_failure but through the
    # dict-driven helper, matching how the DAGs actually call it.
    summary = {"regions_attempted": 1, "regions_succeeded": 1, "regions_with_data": 0}
    raise_if_all_fetches_raised(summary, entity="regions", label="ingest_states")


def test_raise_helper_raises_when_all_tasks_raised():
    summary = {"airports_attempted": 4, "airports_succeeded": 0, "airports_with_data": 0}
    with pytest.raises(RuntimeError, match="all 4"):
        raise_if_all_fetches_raised(summary, entity="airports", label="ingest_flights")
