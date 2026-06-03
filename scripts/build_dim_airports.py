from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request
from pathlib import Path


SOURCE_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airports.csv"

# OpenFlights airports.dat column order (headerless).
_NAME, _CITY, _COUNTRY, _IATA, _ICAO, _LAT, _LON = 1, 2, 3, 4, 5, 6, 7
_ICAO_RE = re.compile(r"[A-Z]{4}")  # 4-letter ICAO airport codes only; rejects OpenFlights's \N sentinel.


def _clean(v: str) -> str:
    return "" if v == r"\N" else v


def build(text: str) -> list[dict]:
    best: dict[str, dict] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8 or not _ICAO_RE.fullmatch(row[_ICAO]):
            continue
        icao = row[_ICAO]
        if icao in best:  # no active/id priority for airports; first occurrence in source order is canonical
            continue
        best[icao] = {
            "icao": icao,
            "iata": _clean(row[_IATA]),
            "name": _clean(row[_NAME]),
            "city": _clean(row[_CITY]),
            "country": _clean(row[_COUNTRY]),
            "lat": row[_LAT],
            "lon": row[_LON],
        }
    return [best[k] for k in sorted(best)]


def main() -> int:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        rows = build(resp.read().decode("utf-8"))
    if not rows:  # never clobber a good committed seed with an empty parse (e.g. upstream returns nothing)
        raise SystemExit("build_dim_airports: parsed 0 airports from source; refusing to overwrite seed")
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["icao", "iata", "name", "city", "country", "lat", "lon"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} airports -> {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
