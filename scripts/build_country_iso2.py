from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "dbt/sancha1090/seeds/dim_hex_country.csv"
FLAG_COUNTRY_JSON = ROOT / "livemap/static/vendor/flag-icons/country.json"
OUT = ROOT / "livemap/static/country-iso2.json"

# dim_hex_country (tar1090 flags.js) names that flag-icons spells differently.
OVERRIDES = {
    "C\u00f4te d\u2019Ivoire": "ci",  # seed uses curly apostrophe U+2019
    "Micronesia, Federated States of": "fm",  # flag-icons: "Federated States of Micronesia"
    "Brunei": "bn",                    # flag-icons: "Brunei Darussalam"
    "Czechia": "cz",                   # flag-icons: "Czech Republic"
    "DR Congo": "cd",                  # flag-icons: "Democratic Republic of the Congo"
    "São Tomé and Príncipe": "st",     # flag-icons: "Sao Tome and Principe"
    "Turkey": "tr",                    # flag-icons: "Türkiye"
    "United States": "us",             # flag-icons: "United States of America"
    "Viet Nam": "vn",                  # flag-icons: "Vietnam"
}
# Non-country sentinels in the tar1090 table -> deliberately no flag.
NON_COUNTRY = {"ICAO (special use)", "ICAO (temporary)"}


def build(seed_names, flagicons_country, overrides=None, non_country=None):
    overrides = OVERRIDES if overrides is None else overrides
    non_country = NON_COUNTRY if non_country is None else non_country
    by_name = {c["name"]: c["code"] for c in flagicons_country}
    out, missing = {}, []
    for name in seed_names:
        if name in non_country:
            continue
        iso2 = overrides.get(name)
        if iso2 is None:
            iso2 = by_name.get(name)
        if iso2 is None:
            missing.append(name)
        else:
            out[name] = iso2
    return out, missing


def _seed_names(path=SEED):
    with open(path, newline="") as fh:
        return sorted({r["country"] for r in csv.DictReader(fh)})


def main() -> int:
    names = _seed_names()
    flagicons = json.loads(FLAG_COUNTRY_JSON.read_text())
    out, missing = build(names, flagicons)
    if missing:
        print(f"UNMAPPED names - add to OVERRIDES or NON_COUNTRY: {missing}", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=0) + "\n")
    print(f"wrote {len(out)} country->iso2 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
