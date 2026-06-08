from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_dim_aircraft_types import build  # noqa: E402

# A slice of the real ICAO Doc 8643 CSV shape: typecode, class, N/EngineType, "MFR, Model".
SAMPLE = (
    'Aircraft TypeDesignator,Class,Number+Engine Type,"MANUFACTURER, Model"\n'
    'B738,LandPlane,2/Jet,"BOEING, 737-800"\n'
    'A359,LandPlane,2/Jet,"AIRBUS, A-350-900 XWB"\n'
    'A388,LandPlane,4/Jet,"AIRBUS, A-380-800"\n'
    'B744,LandPlane,4/Jet,"BOEING, 747-400"\n'
    'DH8D,LandPlane,2/Turboprop/Turboshaft,"DE HAVILLAND CANADA, DHC-8-400"\n'
    'C172,LandPlane,1/Piston,"CESSNA, 172 Skyhawk"\n'
    'R44,Helicopter,1/Piston,"ROBINSON, R-44 Raven"\n'
    'MD11,LandPlane,3/Jet,"BOEING, MD-11"\n'
    'BALL,LighterThanAir,0/None,"BALLOON, generic"\n'  # unclassifiable → dropped
)


def _by_code(rows):
    return {r["typecode"]: r for r in rows}


def test_body_class_mapping():
    rows = _by_code(build(SAMPLE))
    assert rows["A388"]["body_class"] == "quad"      # 4 engines
    assert rows["B744"]["body_class"] == "quad"
    assert rows["A359"]["body_class"] == "widebody"  # 2-jet, curated widebody set
    assert rows["B738"]["body_class"] == "narrowbody"  # 2-jet, not widebody
    assert rows["DH8D"]["body_class"] == "regional"  # 2-turboprop
    assert rows["C172"]["body_class"] == "ga"        # single
    assert rows["R44"]["body_class"] == "heli"       # Helicopter class
    assert rows["MD11"]["body_class"] == "widebody"  # trijet → widebody


def test_engines_parsed_as_int():
    rows = _by_code(build(SAMPLE))
    assert rows["A388"]["engines"] == 4
    assert rows["B738"]["engines"] == 2
    assert rows["C172"]["engines"] == 1


def test_unclassifiable_dropped():
    rows = _by_code(build(SAMPLE))
    assert "BALL" not in rows  # 0 engines / lighter-than-air → no body_class → skipped


def test_sorted_unique_typecodes():
    codes = [r["typecode"] for r in build(SAMPLE)]
    assert codes == sorted(codes)
    assert len(codes) == len(set(codes))
