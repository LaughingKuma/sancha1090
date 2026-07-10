from __future__ import annotations

import logging

import pytest

from include import clickhouse as ch


def _task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("load_to_clickhouse").python_callable


def test_segments_failure_raises(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    with pytest.raises(RuntimeError, match="segments"):
        _task(dagbag)({})


def test_paths_only_failure_warns_and_does_not_raise(dagbag, monkeypatch, caplog):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True, "rows": 5})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    with caplog.at_level(logging.WARNING, logger="ingest_adsblol_routes"):
        result = _task(dagbag)({})
    assert result["segments"]["ok"] and not result["paths"]["ok"]
    assert any("paths" in rec.message for rec in caplog.records)


def test_both_ok_returns_both(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    result = _task(dagbag)({})
    assert result == {"segments": {"ok": True}, "paths": {"ok": True}}
