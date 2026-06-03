from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest


SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airports.csv"

# Nearest-airport snapping anchors: their ICAO must resolve to the right city/country.
ANCHORS = {
    "RJTT": ("Tokyo", "Japan"),
    "RJAA": ("Tokyo", "Japan"),
    "KJFK": ("New York", "United States"),
    "EGLL": ("London", "United Kingdom"),
}


def _rows() -> list[dict]:
    if not SEED.exists():
        pytest.fail(f"seed missing: {SEED}")
    with SEED.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_columns_exact():
    assert list(_rows()[0].keys()) == ["icao", "iata", "name", "city", "country", "lat", "lon"]


def test_icao_is_four_uppercase_letters():
    bad = [r["icao"] for r in _rows() if not re.fullmatch(r"[A-Z]{4}", r["icao"])]
    assert not bad, f"non-ICAO airport codes leaked in: {bad[:10]}"


def test_icao_is_unique_pk():
    icaos = [r["icao"] for r in _rows()]
    dupes = {c for c in icaos if icaos.count(c) > 1}
    assert not dupes, f"duplicate ICAO PKs (dedup failed): {sorted(dupes)}"


def test_latlon_are_floats_in_range():
    for r in _rows():
        lat, lon = float(r["lat"]), float(r["lon"])
        assert -90 <= lat <= 90, f"{r['icao']} lat out of range: {lat}"
        assert -180 <= lon <= 180, f"{r['icao']} lon out of range: {lon}"


@pytest.mark.parametrize(("icao", "expected"), list(ANCHORS.items()))
def test_anchor_airports_resolve(icao, expected):
    by_icao = {r["icao"]: (r["city"], r["country"]) for r in _rows()}
    assert by_icao.get(icao) == expected
