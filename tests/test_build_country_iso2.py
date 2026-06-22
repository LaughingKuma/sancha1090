from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_country_iso2 import build, _seed_names, NON_COUNTRY  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# flag-icons country.json shape: [{"code": "jp", "name": "Japan"}, ...]
FI_SAMPLE = [
    {"code": "jp", "name": "Japan"},
    {"code": "us", "name": "United States"},
]


def test_build_maps_overrides_and_skips_sentinels():
    names = ["Japan", "United States", "C\u00f4te d\u2019Ivoire", "ICAO (temporary)"]
    out, missing = build(names, FI_SAMPLE)
    assert out["Japan"] == "jp"
    assert out["United States"] == "us"   # resolved via OVERRIDES (fi name is "United States of America")
    assert out["C\u00f4te d\u2019Ivoire"] == "ci"  # via OVERRIDES (seed uses curly U+2019)
    assert "ICAO (temporary)" not in out          # NON_COUNTRY -> skipped
    assert missing == []


def test_build_reports_unmapped():
    out, missing = build(["Atlantis"], FI_SAMPLE)
    assert out == {}
    assert missing == ["Atlantis"]


def test_committed_json_covers_every_seed_country():
    mapping = json.loads((ROOT / "livemap/static/country-iso2.json").read_text())
    for name in _seed_names():
        if name in NON_COUNTRY:
            continue
        assert name in mapping, f"no iso2 for {name!r} - add to OVERRIDES in build_country_iso2.py"
    assert set(mapping) == set(_seed_names()) - NON_COUNTRY
    assert all(re.fullmatch(r"[a-z]{2}", v) for v in mapping.values())
