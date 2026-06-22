from __future__ import annotations

import csv
import re
import sys
import urllib.request
from itertools import pairwise
from urllib.parse import urlparse
from pathlib import Path


# Canonical ICAO 24-bit address -> country table; the exact one this feeder's tar1090 map renders
# (richer than dump1090's: carries the territorial carve-outs like Bermuda/Hong Kong it flattens).
SOURCE_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090/master/html/flags.js"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_hex_country.csv"

_ENTRY = re.compile(
    r'start:\s*(0x[0-9A-Fa-f]+)\s*,\s*end:\s*(0x[0-9A-Fa-f]+)\s*,\s*country:\s*"([^"]+)"'
)


def parse(js: str) -> list[tuple[int, int, str]]:
    # File order is priority: flags.js lookup returns the first matching (most-specific) range.
    return [(int(s, 16), int(e, 16), c) for s, e, c in _ENTRY.findall(js)]


def flatten(entries: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    # Collapse the deliberately-overlapping source into a disjoint partition (specific wins), so a
    # single from_base(hex,16) BETWEEN ... join matches exactly one row and never fans out the fact.
    bounds = sorted({b for lo, hi, _ in entries for b in (lo, hi + 1)})
    segments: list[tuple[int, int, str]] = []
    for lo, hi in pairwise(bounds):
        winner = next((c for s, e, c in entries if s <= lo and hi - 1 <= e), None)
        if winner is None:
            continue  # unallocated gap -> no row -> NULL country in the join (correct)
        if segments and segments[-1][2] == winner and segments[-1][1] + 1 == lo:
            segments[-1] = (segments[-1][0], hi - 1, winner)
        else:
            segments.append((lo, hi - 1, winner))
    return segments


def main() -> int:
    parsed = urlparse(SOURCE_URL)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        raise ValueError(f"unsupported source URL: {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:  # noqa: S310 — hardcoded https const, validated above
        entries = parse(resp.read().decode("utf-8"))
    rows = flatten(entries)
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["block_lo", "block_hi", "country"])
        w.writerows(rows)
    print(f"wrote {len(rows)} disjoint blocks from {len(entries)} source ranges -> {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
