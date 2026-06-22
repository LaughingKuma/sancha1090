from __future__ import annotations

import csv
from itertools import pairwise
from pathlib import Path

import pytest


SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_hex_country.csv"


def _rows() -> list[dict]:
    if not SEED.exists():
        pytest.fail(f"seed missing: {SEED}")
    with SEED.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _country_for(hex_str: str) -> list[str]:
    # Mirrors the silver join: from_base(hex,16) BETWEEN block_lo AND block_hi.
    n = int(hex_str, 16)
    return [r["country"] for r in _rows() if int(r["block_lo"]) <= n <= int(r["block_hi"])]


def test_columns_exact():
    assert list(_rows()[0].keys()) == ["block_lo", "block_hi", "country"]


def test_bounds_are_bigint_and_in_24bit_space():
    for r in _rows():
        lo, hi = int(r["block_lo"]), int(r["block_hi"])
        assert 0 <= lo <= hi <= 0xFFFFFF, r


def test_ranges_sorted_and_non_overlapping():
    # Load-bearing: a hex matching two rows would fan out the fct LEFT join and break
    # row-count preservation. Flattening flags.js to a disjoint partition is what guarantees this.
    blocks = [(int(r["block_lo"]), int(r["block_hi"])) for r in _rows()]
    assert blocks == sorted(blocks), "rows must be sorted by block_lo"
    for (_, hi1), (lo2, _) in pairwise(blocks):
        assert lo2 > hi1, f"overlapping blocks: {hi1:#x} >= {lo2:#x}"


@pytest.mark.parametrize(("hex_str", "country"), [
    ("840000", "Japan"),          # lower edge of an un-subdivided block
    ("87ffff", "Japan"),          # upper edge
    ("86abcd", "Japan"),
    ("a12345", "United States"),
    ("3d0000", "Germany"),
    ("790000", "China"),          # China proper, above the Hong Kong carve-out
    ("789abc", "Hong Kong"),      # specific-wins: HK block sits inside China's range
])
def test_hex_resolves_to_exactly_one_country(hex_str, country):
    assert _country_for(hex_str) == [country], f"{hex_str} -> {_country_for(hex_str)}"
