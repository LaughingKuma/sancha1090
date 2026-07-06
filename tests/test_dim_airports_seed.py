from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest


SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airports.csv"

# Nearest-airport snapping anchors: their ICAO must resolve to the right city/country.
ANCHORS = {
    "RJTT": ("Tokyo", "Japan"),
    "RJAA": ("Narita", "Japan"),
    "KJFK": ("New York", "United States"),
    "EGLL": ("London", "United Kingdom"),
}


def _rows() -> list[dict]:
    if not SEED.exists():
        pytest.fail(f"seed missing: {SEED}")
    with SEED.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_columns_exact():
    assert list(_rows()[0].keys()) == [
        "icao", "iata", "name", "city", "country", "lat", "lon", "airport_type", "scheduled_service",
    ]


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


def test_scheduled_service_signal_anchors():
    # The snap gate's load-bearing signal: guard it against upstream reclassification.
    by_icao = {r["icao"]: r["scheduled_service"] for r in _rows()}
    assert by_icao.get("RJTT") == "true"
    assert by_icao.get("RJTK") == "false"


def test_bootstrap_paths_carry_scoped_full_refresh():
    # The schema-changed seed breaks a plain `dbt seed` on an existing table; BOTH bootstrap
    # paths must carry the scoped split or a redeploy fails (drift bit us on PR #94).
    root = Path(__file__).resolve().parent.parent
    for path in (root / "scripts/ch_setup_marts.sh", root / "docker-compose.yml"):
        text = path.read_text()
        assert "--full-refresh --select dim_airports" in text, f"{path.name} lost the scoped full-refresh"
        plain_seed_lines = [ln for ln in text.splitlines()
                            if "dbt seed" in ln and "--full-refresh" not in ln]
        assert all("dim_airports" not in ln or "--exclude dim_airports" in ln for ln in plain_seed_lines), \
            f"{path.name} plain-seeds dim_airports"
