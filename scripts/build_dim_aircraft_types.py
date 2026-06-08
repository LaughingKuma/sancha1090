from __future__ import annotations

import csv
import io
import sys
import urllib.request
from pathlib import Path

# ICAO Doc 8643 type designators (rikgale mirror): typecode, class, "N/EngineType", "MFR, Model".
SOURCE_URL = "https://raw.githubusercontent.com/rikgale/ICAOList/main/ICAOList.csv"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_aircraft_types.csv"

_HELI_CLASSES = {"Helicopter", "Gyrocopter", "Tiltrotor"}

# The source carries engine COUNT but not wake category, so the widebody/narrowbody split (both
# "2/Jet") is curated — only 2-engine widebodies need listing; 4-engine types are quad by count.
_WIDEBODY = {
    "A30B", "A306", "A310", "A3ST", "A332", "A333", "A338", "A339", "A359", "A35K",
    "B762", "B763", "B764", "B772", "B77L", "B77W", "B778", "B779", "B788", "B789", "B78X",
}


def _body_class(typecode: str, klass: str, engines: int | None, etype: str) -> str:
    if klass in _HELI_CLASSES:
        return "heli"
    if engines is None:
        return ""
    if engines >= 4:
        return "quad"
    if engines == 3:
        return "widebody"  # surviving trijets (MD-11/DC-10) read as widebody
    if engines == 2:
        if "Jet" in etype:
            return "widebody" if typecode in _WIDEBODY else "narrowbody"
        return "regional"  # twin turboprop / piston
    if engines == 1:
        return "ga"
    return ""


def build(text: str) -> list[dict]:
    best: dict[str, dict] = {}
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # header
    for row in reader:
        if len(row) < 3 or not row[0]:
            continue
        typecode = row[0].strip().upper()
        klass = row[1].strip()
        parts = row[2].split("/")
        engines = int(parts[0]) if parts and parts[0].strip().isdigit() else None
        etype = parts[1].strip() if len(parts) > 1 else ""
        body_class = _body_class(typecode, klass, engines, etype)
        # seed contract: engines is always a positive int (the RW loader casts it) — drop
        # anything unclassifiable or without a parsed engine count; the MV's LEFT JOIN then
        # returns null and the frontend falls back to the generic glyph.
        if not body_class or engines is None or engines < 1:
            continue
        # first designator wins (the list is one row per designator; guard against any dup)
        best.setdefault(typecode, {
            "typecode": typecode,
            "engines": engines,
            "body_class": body_class,
        })
    return [best[k] for k in sorted(best)]


def main() -> int:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        rows = build(resp.read().decode("utf-8", errors="replace"))
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with SEED.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["typecode", "engines", "body_class"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} aircraft types -> {SEED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
