from __future__ import annotations

from scripts.build_dim_airports import build

AIRPORTS_HDR = ('"id","ident","type","name","latitude_deg","longitude_deg","elevation_ft",'
                '"continent","iso_country","iso_region","municipality","scheduled_service",'
                '"icao_code","iata_code","gps_code","local_code","home_link","wikipedia_link","keywords"')

COUNTRIES = '\n'.join([
    '"id","code","name","continent","wikipedia_link","keywords"',
    '1,"JP","Japan","AS","",""',
    '2,"US","United States","NA","",""',
])


def _airports(*rows: str) -> str:
    return "\n".join([AIRPORTS_HDR, *rows])


def _row(ident="", typ="large_airport", name="X", lat="35.0", lon="139.0", country="JP",
         city="Tokyo", sched="yes", icao="", iata=""):
    return (f'1,"{ident}","{typ}","{name}",{lat},{lon},20,"AS","{country}","JP-13","{city}",'
            f'"{sched}","{icao}","{iata}","","","","",""')


def test_maps_ourairports_columns():
    rows = build(_airports(_row(icao="RJTT", iata="HND", name="Tokyo Haneda", city="Tokyo")), COUNTRIES)
    assert rows == [{"icao": "RJTT", "iata": "HND", "name": "Tokyo Haneda", "city": "Tokyo",
                     "country": "Japan", "lat": "35.0", "lon": "139.0",
                     "airport_type": "large_airport", "scheduled_service": "true"}]


def test_scheduled_service_no_maps_false():
    rows = build(_airports(_row(icao="RJTK", typ="medium_airport", sched="no")), COUNTRIES)
    assert rows[0]["scheduled_service"] == "false"


def test_drops_closed_and_balloonport():
    rows = build(_airports(_row(icao="RJAA", typ="closed"), _row(icao="RJBB", typ="balloonport")), COUNTRIES)
    assert rows == []


def test_icao_from_ident_when_icao_code_blank():
    rows = build(_airports(_row(ident="RJFF")), COUNTRIES)
    assert rows[0]["icao"] == "RJFF"


def test_rejects_non_icao_idents():
    rows = build(_airports(_row(ident="JP-0241"), _row(ident="03N")), COUNTRIES)
    assert rows == []


def test_duplicate_icao_prefers_larger_type_then_scheduled():
    rows = build(_airports(
        _row(icao="RJZZ", typ="heliport", name="Heli", sched="no"),
        _row(icao="RJZZ", typ="medium_airport", name="Field", sched="no"),
        _row(icao="RJZZ", typ="medium_airport", name="Afield", sched="yes"),
    ), COUNTRIES)
    assert len(rows) == 1
    assert (rows[0]["name"], rows[0]["scheduled_service"]) == ("Afield", "true")


def test_duplicate_icao_name_tiebreak_when_type_and_sched_tie():
    rows = build(_airports(
        _row(icao="RJZY", typ="small_airport", name="Bravo Field", sched="no"),
        _row(icao="RJZY", typ="small_airport", name="Alpha Field", sched="no"),
    ), COUNTRIES)
    assert len(rows) == 1
    assert rows[0]["name"] == "Alpha Field"


def test_unmapped_country_falls_back_to_code():
    rows = build(_airports(_row(icao="RJTT", country="XZ")), COUNTRIES)
    assert rows[0]["country"] == "XZ"


def test_output_sorted_by_icao():
    rows = build(_airports(_row(icao="RJBB"), _row(icao="RJAA")), COUNTRIES)
    assert [r["icao"] for r in rows] == ["RJAA", "RJBB"]


def test_main_refuses_to_overwrite_seed_on_small_parse(monkeypatch, tmp_path):
    import scripts.build_dim_airports as g
    import pytest

    monkeypatch.setattr(g, "_fetch", lambda url: COUNTRIES if "countries" in url else _airports(_row(icao="RJTT")))
    sentinel = tmp_path / "dim_airports.csv"
    sentinel.write_text("icao\nKEEP\n")
    monkeypatch.setattr(g, "SEED", sentinel)
    with pytest.raises(SystemExit):
        g.main()
    assert sentinel.read_text() == "icao\nKEEP\n"
