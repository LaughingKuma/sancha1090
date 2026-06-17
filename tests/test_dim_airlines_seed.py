from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest


SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airlines.csv"

# The doc's "top airlines over Tokyo" sanity targets — their ICAO designator must resolve cleanly.
# CPA/EVA/CAL also guard the Taiwan/HK country restoration; AIH guards the stale-code fix (OpenFlights
# had it as a defunct "Alpine Air Chile"; ICAO reassigned AIH to Air Incheon). BOX guards the mashed-name
# cleanup (Mictronics "Aerologicleipzig"); RAC guards the code-level fix (Mictronics has it as Icar Air).
ANCHORS = {
    "ANA": ("All Nippon Airways", "Japan"),
    "JAL": ("Japan Airlines", "Japan"),
    "CPA": ("Cathay Pacific", "Hong Kong SAR of China"),
    "UAL": ("United Airlines", "United States"),
    "EVA": ("EVA Air", "Taiwan"),
    "CAL": ("China Airlines", "Taiwan"),
    "AIH": ("Air Incheon", "South Korea"),
    "BOX": ("AeroLogic", "Germany"),
    "RAC": ("Ryukyu Air Commuter", "Japan"),
    # v5.11 Wikidata name batch: AFL/SVA/AIC guard accepted brand/suffix cleanups; ATG/APJ guard the
    # hand-curated _NAME entries (Wikidata's "Aerotrans" wrongly drops cargo, "Peach" is too terse);
    # HTA/ORK guard rejected proposals where the Wikidata label was a DIFFERENT airline (must stay
    # on the Mictronics name).
    "AFL": ("Aeroflot", "Russia"),
    "SVA": ("Saudia", "Saudi Arabia"),
    "AIC": ("Air India", "India"),
    "ATG": ("AeroTransCargo", "Moldova"),
    "APJ": ("Peach Aviation", "Japan"),
    "HTA": ("Heli Transair European Air Services", "Germany"),
    "ORK": ("Orca Airways", "Canada"),
}


def _rows() -> list[dict]:
    if not SEED.exists():
        pytest.fail(f"seed missing: {SEED}")
    with SEED.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_columns_exact():
    assert list(_rows()[0].keys()) == ["icao", "iata", "name", "callsign", "country", "active"]


def test_icao_is_three_uppercase_letters():
    bad = [r["icao"] for r in _rows() if not re.fullmatch(r"[A-Z]{3}", r["icao"])]
    assert not bad, f"non-designator ICAO codes leaked in: {bad[:10]}"


def test_icao_is_unique_pk():
    icaos = [r["icao"] for r in _rows()]
    dupes = {c for c in icaos if icaos.count(c) > 1}
    assert not dupes, f"duplicate ICAO PKs (dedup failed): {sorted(dupes)}"


def test_active_flag_normalized():
    assert {r["active"] for r in _rows()} <= {"Y", "N"}


@pytest.mark.parametrize(("icao", "expected"), list(ANCHORS.items()))
def test_anchor_airlines_resolve(icao, expected):
    by_icao = {r["icao"]: (r["name"], r["country"]) for r in _rows()}
    assert by_icao.get(icao) == expected
