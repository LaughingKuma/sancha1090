from pathlib import Path
import pytest
from include.swim_parser import parse_envelope

FIX = Path(__file__).parent / "fixtures" / "swim"
# Fixtures are gitignored real flight data (LADD/showcase) — local-only; skip when absent so CI stays green.
pytestmark = pytest.mark.skipif(not (FIX / "msg_008.xml").exists(),
                                reason="swim fixtures are gitignored / local-only")

def test_envelope_yields_odbearing_rows():
    rows = parse_envelope((FIX / "msg_008.xml").read_bytes())   # contains a flightPlanAmendment
    assert rows                                                  # at least one kept fltdMessage
    amd = [r for r in rows if r["msg_type"] == "flightPlanAmendmentInformation"]
    assert amd, "amendment present in msg_008"
    r = amd[0]
    assert r["acid"] and len(r["dep_point"]) == 4 and r["dep_point_kind"] == "airport"
    assert r["arr_point"] and len(r["arr_point"]) == 4
    assert r["msg_timestamp"] is not None      # @sourceTimeStamp = the version
    assert r["gufi"] and r["flight_ref"]        # flight-plan-class rows carry both
    assert "fltdMessage" in r["raw_xml"]        # per-message raw retained

def test_trackinformation_is_filtered_out():
    # msg_000's first fltdMessage is a trackInformation (DAL495 KATL→KSFO) — must be dropped by KEEP_MSGTYPES.
    rows = parse_envelope((FIX / "msg_000.xml").read_bytes())
    assert all(r["msg_type"] != "trackInformation" for r in rows)

def test_no_fltdmessage_returns_empty_list():
    # real envelopes always declare the ds namespace (see msg_000.xml); an unbound prefix isn't well-formed XML.
    assert parse_envelope(b'<ds:tfmDataService xmlns:ds="urn:us:gov:dot:faa:atm:tfm:tfmdataservice"/>') == []
