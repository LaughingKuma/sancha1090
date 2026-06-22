from __future__ import annotations

import pytest

from scripts.build_dim_airports import build


# A trimmed airports.dat fixture (14-col OpenFlights rows: ID,Name,City,Country,IATA,ICAO,Lat,Lon,...).
FIXTURE = "\n".join([
    '2359,"Tokyo Haneda Intl","Tokyo","Japan","HND","RJTT",35.552299,139.779999,35,9,"N","Asia/Tokyo","airport","OurAirports"',
    '2358,"Narita Intl","Tokyo","Japan","NRT","RJAA",35.764702,140.386002,141,9,"N","Asia/Tokyo","airport","OurAirports"',
    '3797,"John F Kennedy Intl","New York","United States","JFK","KJFK",40.639801,-73.7789,13,-5,"A","America/New_York","airport","OurAirports"',
    '507,"London Heathrow","London","United Kingdom","LHR","EGLL",51.4706,-0.461941,83,0,"E","Europe/London","airport","OurAirports"',
    r'9999,"No ICAO Field","Nowhere","Nowhere","NOI","\N",1.0,2.0,0,0,"U","\N","airport","OurAirports"',
])


def test_build_parses_anchor_airports():
    by_icao = {r["icao"]: r for r in build(FIXTURE)}
    assert {"RJTT", "RJAA", "KJFK", "EGLL"} <= set(by_icao)
    assert by_icao["RJTT"]["city"] == "Tokyo"
    assert float(by_icao["RJTT"]["lat"]) == pytest.approx(35.5523, abs=1e-3)
    assert float(by_icao["KJFK"]["lon"]) == pytest.approx(-73.7789, abs=1e-3)


def test_build_drops_missing_icao():
    icaos = {r["icao"] for r in build(FIXTURE)}
    assert all(icaos)               # no empty strings
    assert r"\N" not in icaos       # \N rows dropped, not kept


def test_build_icao_unique():
    icaos = [r["icao"] for r in build(FIXTURE)]
    assert len(icaos) == len(set(icaos))
