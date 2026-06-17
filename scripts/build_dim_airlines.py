from __future__ import annotations

import csv
import json
import re
import sys
import urllib.request
from urllib.parse import urlparse
from pathlib import Path


# Mictronics readsb operators DB — the source tar1090/readsb/adsbexchange use; tracks current ICAO
# Doc 8585 designators, unlike the frozen OpenFlights airlines.dat (stale codes, e.g. AIH was a
# defunct "Alpine Air Chile" but ICAO reassigned it to Air Incheon).
SOURCE_URL = "https://raw.githubusercontent.com/Mictronics/readsb/master/webapp/src/db/operators.json"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airlines.csv"

_DESIGNATOR = re.compile(r"[A-Z]{3}")

# Mictronics flattens Taiwan and Hong Kong carriers to "China"; restore the registering jurisdiction
# (separate CAA + reg prefix) for the ones that fly the Tokyo box. Codes verified against the source.
_TAIWAN = {"CAL", "EVA", "FEA", "MDA", "SJX", "TNA", "TTW", "UIA"}
_HONG_KONG = {"AHK", "CPA", "CRK", "HDA", "HKC", "HKE", "HKG", "HKJ", "JKT"}

# Brand/common name where Mictronics carries the verbose legal entity.
_NAME = {
    "EVA": "EVA Air",            # Eva Airways Corporation
    "CPA": "Cathay Pacific",     # Cathay Pacific Airways
    "UIA": "UNI Air",            # Uni Airways Corporation
    "TNA": "TransAsia Airways",  # Transasia Airways
    "KAL": "Korean Air",         # Korean Air Lines.
}


def _clean(name: str) -> str:
    # Mictronics has SQL-escape artifacts: doubled apostrophes and trailing periods.
    return name.replace("''", "'").strip().rstrip(".").strip()


def build(text: str) -> list[dict]:
    raw = json.loads(text)
    rows = []
    for icao in sorted(raw):
        if not _DESIGNATOR.fullmatch(icao):
            continue
        e = raw[icao]
        country = e.get("c") or ""
        if icao in _TAIWAN:
            country = "Taiwan"
        elif icao in _HONG_KONG:
            country = "Hong Kong SAR of China"
        rows.append({
            "icao": icao,
            "iata": "",  # Mictronics carries no IATA; column kept for schema stability (unused downstream)
            "name": _NAME.get(icao, _clean(e.get("n") or "")),
            "callsign": (e.get("r") or "").strip(),
            "country": country,
            "active": "Y",  # source curates to current operators; no per-row active flag
        })
    return rows


def main() -> int:
    parsed = urlparse(SOURCE_URL)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        raise ValueError(f"unsupported source URL: {SOURCE_URL}")
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:  # noqa: S310 — hardcoded https const, validated above
        rows = build(resp.read().decode("utf-8"))
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["icao", "iata", "name", "callsign", "country", "active"],
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} airlines -> {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
