from __future__ import annotations

import pytest

from include.vrs_routes import fetch_routes_csv, parse_routes

HDR = "Callsign,Code,Number,AirlineCode,AirportCodes"


def _csv(*rows: str) -> str:
    return "\n".join([HDR, *rows])


def test_rejects_non_mirror_url():
    with pytest.raises(ValueError, match="unsupported source URL"):
        fetch_routes_csv("https://example.com/routes.csv")


def test_header_drift_raises():
    with pytest.raises(ValueError, match="header drift"):
        parse_routes("Callsign,Code,Nope\nX,Y,Z", min_rows=1)


def test_bom_on_header_tolerated():
    rows = parse_routes("\ufeff" + _csv("SFJ43,SFJ,43,SFJ,RJTT-RJFF"), min_rows=1)
    assert rows == [["SFJ43", "SFJ", "43", "SFJ", "RJTT-RJFF"]]


def test_short_fetch_refused():
    with pytest.raises(ValueError, match="refusing to load"):
        parse_routes(_csv("SFJ43,SFJ,43,SFJ,RJTT-RJFF"), min_rows=2)


def test_skips_blank_callsign_or_route():
    rows = parse_routes(_csv(",X,1,X,RJTT-RJFF", "KAP100,KAP,100,KAP,", "ANA1,ANA,1,ANA,RJTT-ROAH"), min_rows=1)
    assert rows == [["ANA1", "ANA", "1", "ANA", "RJTT-ROAH"]]
