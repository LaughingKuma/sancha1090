from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request
from pathlib import Path


SOURCE_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airlines.csv"

# OpenFlights airlines.dat column order (headerless).
_AIRLINE_ID, _NAME, _ALIAS, _IATA, _ICAO, _CALLSIGN, _COUNTRY, _ACTIVE = range(8)
_DESIGNATOR = re.compile(r"[A-Z]{3}")


def _clean(v: str) -> str:
    return "" if v == r"\N" else v


def build(text: str) -> list[dict]:
    best: dict[str, tuple[int, dict]] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8 or not _DESIGNATOR.fullmatch(row[_ICAO]):
            continue
        icao = row[_ICAO]
        active = "Y" if row[_ACTIVE] == "Y" else "N"
        airline_id = int(row[_AIRLINE_ID])
        # Type-1 dedup: an active carrier wins its designator, then the lowest (canonical) id.
        rank = (active != "Y", airline_id)
        if icao not in best or rank < best[icao][0]:
            best[icao] = (rank, {
                "icao": icao,
                "iata": _clean(row[_IATA]),
                "name": _clean(row[_NAME]),
                "callsign": _clean(row[_CALLSIGN]),
                "country": _clean(row[_COUNTRY]),
                "active": active,
            })
    return [best[k][1] for k in sorted(best)]


def main() -> int:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        rows = build(resp.read().decode("utf-8"))
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["icao", "iata", "name", "callsign", "country", "active"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} airlines -> {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
