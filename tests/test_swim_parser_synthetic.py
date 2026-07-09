from pathlib import Path
from datetime import datetime
from include.swim_parser import parse_envelope

# A committed, hand-built (NON-real, no live flight data) envelope so the parser is exercised in CI even
# though the real fixtures under tests/fixtures/swim/ are gitignored. One kept amendment + one filtered track.
SYNTH = (Path(__file__).parent / "fixtures" / "swim_synthetic" / "amendment.xml").read_bytes()


def test_parse_synthetic_amendment_full_shape():
    rows = parse_envelope(SYNTH)
    assert len(rows) == 1                    # the trackInformation is filtered out; only the amendment kept
    r = rows[0]
    assert r["msg_type"] == "flightPlanAmendmentInformation"
    assert r["acid"] == "TEST123" and r["gufi"] == "SYNTHGUFI01" and r["flight_ref"] == "900000001"
    assert r["computer_id"] == "KZAB/123"
    assert r["dep_point"] == "KABQ" and r["dep_point_kind"] == "airport"
    assert r["arr_point"] == "KROW" and r["arr_point_kind"] == "airport"
    assert r["filed_departure_time"] == datetime(2020, 1, 1, 1, 0, 0)   # igtd, tz-normalized to naive UTC
    assert r["msg_timestamp"] == datetime(2020, 1, 1, 0, 0, 0)          # @sourceTimeStamp = the version
    assert "fltdMessage" in r["raw_xml"]


def test_synthetic_track_is_filtered():
    assert all(r["msg_type"] != "trackInformation" for r in parse_envelope(SYNTH))


def test_partial_computerid_renders_without_none():
    # only facilityIdentifier present → computer_id must be "KZAB", never "KZAB/None".
    variant = SYNTH.replace(b"<nxce:idNumber>123</nxce:idNumber>", b"")
    rows = parse_envelope(variant)
    assert rows[0]["computer_id"] == "KZAB"


def test_no_fltdmessage_returns_empty_list():
    assert parse_envelope(b'<ds:tfmDataService xmlns:ds="urn:x"/>') == []
