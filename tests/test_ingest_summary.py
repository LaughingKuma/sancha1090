from __future__ import annotations

import pytest

from include.ingest_summary import all_landings_failed, raise_if_all_landings_failed


def test_normal_run_with_data_is_not_a_failure():
    # One region attempted, one landed data -> healthy, no raise.
    assert all_landings_failed(attempted=1, with_data=1) is False


def test_partial_failure_is_tolerated():
    # Some fetches failed but at least one landed -> not a wholesale failure (a
    # single-region blip must not red the whole ingest).
    assert all_landings_failed(attempted=3, with_data=1) is False


def test_every_fetch_failed_is_a_wholesale_failure():
    # The 2026-06-30 incident: every fetch_region raised (manifest DB unreachable),
    # nothing landed -> the DAG must NOT report green.
    assert all_landings_failed(attempted=3, with_data=0) is True


def test_no_regions_attempted_is_not_a_failure():
    # Nothing to do (empty mapping) is not an ingest failure.
    assert all_landings_failed(attempted=0, with_data=0) is False


def test_raise_helper_raises_on_wholesale_failure():
    summary = {"regions_attempted": 2, "regions_with_data": 0}
    with pytest.raises(RuntimeError, match="all 2"):
        raise_if_all_landings_failed(summary, entity="regions", label="ingest_states")


def test_raise_helper_silent_on_healthy_summary():
    summary = {"airports_attempted": 5, "airports_with_data": 3}
    # Must not raise.
    raise_if_all_landings_failed(summary, entity="airports", label="ingest_flights")
