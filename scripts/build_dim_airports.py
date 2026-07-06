from __future__ import annotations

import csv
import io
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

AIRPORTS_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/airports.csv"
COUNTRIES_URL = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/countries.csv"
SEED = Path(__file__).resolve().parent.parent / "dbt/sancha1090/seeds/dim_airports.csv"

_ICAO_RE = re.compile(r"[A-Z]{4}")
_DROP_TYPES = {"closed", "balloonport"}
# Dedup priority on a shared ICAO: bigger field wins, then scheduled service, then lexical name.
_TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2, "seaplane_base": 3, "heliport": 4}
_COLUMNS = ["icao", "iata", "name", "city", "country", "lat", "lon", "airport_type", "scheduled_service"]


def _countries(text: str) -> dict[str, str]:
    return {r["code"]: r["name"] for r in csv.DictReader(io.StringIO(text)) if r.get("code") and r.get("name")}


def build(airports_text: str, countries_text: str) -> list[dict]:
    names = _countries(countries_text)
    best: dict[str, tuple[tuple, dict]] = {}
    for r in csv.DictReader(io.StringIO(airports_text)):
        typ = r.get("type", "")
        if typ in _DROP_TYPES or typ not in _TYPE_RANK:
            continue
        icao = r.get("icao_code") or r.get("ident") or ""
        if not _ICAO_RE.fullmatch(icao):
            continue
        sched = r.get("scheduled_service") == "yes"
        rank = (_TYPE_RANK[typ], 0 if sched else 1, r.get("name", ""))
        row = {
            "icao": icao, "iata": r.get("iata_code", ""), "name": r.get("name", ""),
            "city": r.get("municipality", ""), "country": names.get(r.get("iso_country", ""), r.get("iso_country", "")),
            "lat": r.get("latitude_deg", ""), "lon": r.get("longitude_deg", ""),
            "airport_type": typ, "scheduled_service": "true" if sched else "false",
        }
        if icao not in best or rank < best[icao][0]:
            best[icao] = (rank, row)
    return [best[k][1] for k in sorted(best)]


def _fetch(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        raise ValueError(f"unsupported source URL: {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    rows = build(_fetch(AIRPORTS_URL), _fetch(COUNTRIES_URL))
    # Never clobber a good committed seed with an empty/near-empty parse.
    if len(rows) < 10_000:
        raise SystemExit(f"build_dim_airports: parsed only {len(rows)} airports; refusing to overwrite seed")
    with SEED.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} airports to {SEED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
