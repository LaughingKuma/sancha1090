import xml.etree.ElementTree as ET     # stdlib; no lxml dependency needed
from datetime import datetime, timezone

NS = {"fdm": "urn:us:gov:dot:faa:atm:tfm:flightdata",
      "nxce": "urn:us:gov:dot:faa:atm:tfm:tfmdatacoreelements",
      "nxcm": "urn:us:gov:dot:faa:atm:tfm:flightdatacommonmessages"}
def _q(p): ns, t = p.split(":"); return f"{{{NS[ns]}}}{t}"

# Drop trackInformation (position firehose, 84% of volume, same O/D); keep the flight-plan-class events.
KEEP_MSGTYPES = {"flightPlanInformation", "flightPlanAmendmentInformation", "FlightRoute",
                 "departureInformation", "arrivalInformation", "FlightModify", "FlightTimes"}

def _txt(el, path):
    f = el.find(path, NS) if el is not None else None
    return f.text.strip() if f is not None and f.text else None

def _ts(s):
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    # a tz-less stamp is UTC by NAS convention — never let astimezone() reinterpret it in the host tz.
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)

def _endpoint(qa, side):
    # discriminate an ICAO airport from a lat/long / fix form so non-airport endpoints don't masquerade as ICAO.
    pt = qa.find(_q(f"nxce:{side}")) if qa is not None else None
    if pt is None or len(pt) == 0:
        return None, "unknown", None
    tag = pt[0].tag.split("}")[-1]
    raw = ("".join(pt[0].itertext()).strip() or None)
    if tag == "airport":
        return (raw.upper() if raw else None), "airport", raw
    if tag in ("latitudeLongitude", "latLong"):
        return None, "latlon", raw
    if tag == "fixRadialDistance":
        return None, "fixradial", raw
    if tag in ("fix", "namedFix"):
        return None, "fix", raw
    return None, "unknown", raw

def _one(m):
    mt = m.get("msgType")
    if mt not in KEEP_MSGTYPES:
        return None
    qa = m.find(f".//{_q('nxcm:qualifiedAircraftId')}")
    dep_attr, arr_attr = m.get("depArpt"), m.get("arrArpt")
    d_icao, d_kind, d_raw = _endpoint(qa, "departurePoint")
    a_icao, a_kind, a_raw = _endpoint(qa, "arrivalPoint")
    cid_fac = _txt(m, f".//{_q('nxce:computerId')}/{_q('nxce:facilityIdentifier')}")
    cid_num = _txt(m, f".//{_q('nxce:computerId')}/{_q('nxce:idNumber')}")
    igtd = _txt(m, f".//{_q('nxce:igtd')}")
    eta = _txt(m, f".//{_q('nxce:eta')}") or _txt(m, f".//{_q('nxce:timeOfArrival')}")
    return {
        "gufi": _txt(m, f".//{_q('nxce:gufi')}"),
        "flight_ref": m.get("flightRef"),
        "acid": (m.get("acid") or _txt(m, f".//{_q('nxce:aircraftId')}")),
        "computer_id": ("/".join(p for p in (cid_fac, cid_num) if p) or None),  # never "X/None" on a partial pair
        "msg_type": mt,
        # attribute O/D is authoritative when present (ICAO); nested airport is the fallback
        "dep_point": (dep_attr.upper() if dep_attr else d_icao),
        "dep_point_kind": ("airport" if dep_attr else d_kind), "dep_point_raw": (dep_attr or d_raw),
        "arr_point": (arr_attr.upper() if arr_attr else a_icao),
        "arr_point_kind": ("airport" if arr_attr else a_kind), "arr_point_raw": (arr_attr or a_raw),
        "filed_departure_time": _ts(igtd), "filed_departure_time_raw": igtd,
        "filed_arrival_time": _ts(eta), "filed_arrival_time_raw": eta,
        "msg_timestamp": _ts(m.get("sourceTimeStamp")),   # intrinsic amendment version
        "raw_xml": ET.tostring(m, encoding="unicode"),
    }

def parse_envelope(xml: bytes):
    root = ET.fromstring(xml)
    return [r for m in root.iter(_q("fdm:fltdMessage")) for r in (_one(m),) if r is not None]
