from __future__ import annotations

import httpx
import pytest

from include.opensky_client import OpenSkyClient


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://opensky-network.org/api/flights/arrival")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


def _raiser(exc):
    def _raise(*_args, **_kwargs):
        raise exc
    return _raise


def test_flights_404_means_empty_window(monkeypatch):
    client = OpenSkyClient()
    monkeypatch.setattr(client, "_request", _raiser(_http_error(404)))
    assert client.get_flights_arrival("RJTT", 1000, 2000) == []
    assert client.get_flights_departure("RJTT", 1000, 2000) == []


def test_flights_other_http_errors_propagate(monkeypatch):
    client = OpenSkyClient()
    monkeypatch.setattr(client, "_request", _raiser(_http_error(403)))
    with pytest.raises(httpx.HTTPStatusError):
        client.get_flights_arrival("RJTT", 1000, 2000)


def test_flights_window_over_two_days_rejected():
    client = OpenSkyClient()
    with pytest.raises(ValueError, match="2-day"):
        client.get_flights_arrival("RJTT", 0, 2 * 86400 + 1)


def test_flights_inverted_window_rejected():
    client = OpenSkyClient()
    with pytest.raises(ValueError, match="after begin"):
        client.get_flights_departure("RJTT", 2000, 1000)


def test_flights_params_passed_through(monkeypatch):
    captured = {}

    def fake_request(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return [{"icao24": "86d594"}]

    client = OpenSkyClient()
    monkeypatch.setattr(client, "_request", fake_request)
    rows = client.get_flights_departure("RJAA", 100, 200)
    assert rows == [{"icao24": "86d594"}]
    assert captured["path"] == "/flights/departure"
    assert captured["params"] == {"airport": "RJAA", "begin": 100, "end": 200}
