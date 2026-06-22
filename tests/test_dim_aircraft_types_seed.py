from __future__ import annotations

import csv
from pathlib import Path

import pytest

SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_aircraft_types.csv"

VALID_CLASSES = {"quad", "widebody", "narrowbody", "regional", "ga", "heli"}

# Common-over-Tokyo types whose silhouette class must resolve correctly.
ANCHORS = {
    "A388": "quad",
    "B744": "quad",
    "B77W": "widebody",
    "B789": "widebody",
    "A359": "widebody",
    "B738": "narrowbody",
    "A20N": "narrowbody",
    "DH8D": "regional",
    "C172": "ga",
}


def _rows() -> list[dict]:
    if not SEED.exists():
        pytest.fail(f"seed missing: {SEED}")
    with SEED.open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_columns_exact():
    assert list(_rows()[0].keys()) == ["typecode", "engines", "body_class", "model_name"]


def test_anchor_model_names():
    assert all(r["model_name"] for r in _rows()), "model_name must never be empty (regen drift tripwire)"
    rows = {r["typecode"]: r["model_name"] for r in _rows()}
    assert rows.get("B738") == "BOEING 737-800"
    assert rows.get("DA40"), "DA40 must carry a model name (issue #34 anchor)"
    assert "DA" in rows["DA40"].upper()


def test_body_class_values_known():
    bad = {r["body_class"] for r in _rows()} - VALID_CLASSES
    assert not bad, f"unexpected body_class values: {bad}"


def test_typecode_is_unique_pk():
    codes = [r["typecode"] for r in _rows()]
    dups = {c for c in codes if codes.count(c) > 1}
    assert not dups, f"duplicate typecodes: {list(dups)[:10]}"


def test_engines_is_positive_int():
    bad = [r["typecode"] for r in _rows() if not r["engines"].isdigit() or int(r["engines"]) < 1]
    assert not bad, f"bad engine counts: {bad[:10]}"


def test_anchor_types_classified():
    rows = {r["typecode"]: r["body_class"] for r in _rows()}
    for code, expected in ANCHORS.items():
        assert rows.get(code) == expected, f"{code}: expected {expected}, got {rows.get(code)}"
