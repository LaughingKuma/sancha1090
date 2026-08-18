import datetime
import json
import sys
import time
from collections import Counter, namedtuple
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _livemap_loader import PUBLIC_ENV, load_livemap_module

ROWS = Path(__file__).resolve().parents[1] / "fixtures" / "workbench" / "rows"

# the recon SELECT's column order (workbench.py _INSTANCES_SELECT + tier cols), named once
Inst = namedtuple("Inst", "flight_id day start_time end_time icao24 registration typecode callsign airline "
                          "o_icao o_iata o_city d_icao d_iata d_city tier gap_s n_points is_mil")
Flag = namedtuple("Flag", Inst._fields + ("flag_class", "detail"))


def rows(name):
    return json.loads((ROWS / f"{name}.json").read_text())


def _iso(s):
    return datetime.datetime.fromisoformat(s) if s else None


def _inst(r):
    # the builder reads naive UTC datetimes at the start/end slots; JSON carries them as ISO text
    return Inst(*r[:2], _iso(r[2]), _iso(r[3]), *r[4:])


def _up(v):
    return (v or "").upper()


def _utc(dt):
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


INSTANCES = [_inst(r) for r in rows("instances")]
_BY_ID = {r.flight_id: r for r in INSTANCES}
# flags.json carries [flight_id, class, detail]; the instance columns come from the instance row itself
FLAGS = [Flag(*_BY_ID[fid], cls, detail) for fid, cls, detail in rows("flags")]
FID = INSTANCES[0].flight_id  # ANA1 — the flight path.json answers, and the spotlight/log specs focus
PATH = [tuple(p) for p in rows("path")]
PATHED = {r.flight_id for r in INSTANCES if r.tier in ("settled", "estimated")}
HEAD = datetime.date(2026, 7, 28)  # settlement head: later start days classify as provisional, not settled_empty


def _in_day(p, r):
    return str(p["day_from"]) <= r.day <= str(p["day_to"])


def _matches(p, r):
    return ((not p["callsign"] or _up(r.callsign).strip() == p["callsign"])
            and (not p["airline"] or r.airline == p["airline"])
            and (not p["hex"] or r.icao24 == p["hex"])
            and (not p["reg"] or _up(r.registration) == p["reg"])
            and (not p["airport"] or p["airport"] in {_up(r.o_icao), _up(r.o_iata), _up(r.d_icao), _up(r.d_iata)})
            and (not p["type"] or r.typecode == p["type"])
            and (not p["od_o"] or (r.o_iata or r.o_icao) == p["od_o"])
            and (not p["od_d"] or (r.d_iata or r.d_icao) == p["od_d"])
            and (not p["military"] or r.is_mil)
            and _in_day(p, r))


def install(app_mod):
    wb, store = app_mod.wb, app_mod.wb_store
    airlines = rows("airlines")
    services = rows("services")
    trends = rows("trends")
    est, cov, search = rows("estimates"), rows("coverage"), rows("search")
    summary = wb.shape_summary(rows("summary"), True, True, True)
    estimates = wb.shape_estimates(est["headline"], est["daily"], est["mix"], est["outcomes"], True)
    coverage = wb.shape_coverage(cov["tier_daily"], cov["gap"], cov["observed"])

    def fetch_airlines(q, limit, offset):
        hit = [r for r in airlines if q.lower() in r[0].lower()]
        return {"airlines": [wb.shape_airline_row(r, True) for r in hit[offset:offset + limit]],
                "total": len(hit), "limit": limit, "offset": offset}

    def fetch_services(airline, q, limit, offset):
        # leading column is the airline the real query binds; sliced off before shaping
        hit = [r[1:] for r in services["rows"] if (r[0] == airline if airline else r[1].startswith(q))]
        top_od = wb.group_top_od(services["top_od"])
        return {"services": [wb.shape_service_row(r, True, top_od) for r in hit[offset:offset + limit]],
                "total": len(hit), "limit": limit, "offset": offset}

    def fetch_instances(callsign, airline, hex_, reg, airport, od, type_, military, day_from, day_to,
                        sort, limit, offset):
        p = wb.instances_params(callsign, airline, hex_, reg, airport, od, type_, military, day_from, day_to)
        # the od breakdown counts the list before its own od filter narrows it
        base = [r for r in INSTANCES if _matches(p | {"od_o": "", "od_d": ""}, r)]
        hit = [r for r in base if _matches(p, r)]
        if sort == "day_asc":
            hit = hit[::-1]
        od_n = Counter((r.o_iata or r.o_icao, r.d_iata or r.d_icao) for r in base)
        od_rows = sorted(((o, d, n) for (o, d), n in od_n.items()), key=lambda t: (-t[2], t[0], t[1]))[:8]
        return {"instances": [wb.shape_instance_row(r) for r in hit[offset:offset + limit]],
                "od_breakdown": wb.shape_od_breakdown(od_rows),
                "total": len(hit), "limit": limit, "offset": offset}

    def fetch_trends(dim, _from, _to, limit, offset):
        t = trends[dim]
        return wb.shape_trends(dim, t["rank"][offset:offset + limit], t["series"], len(t["rank"]), limit, offset)

    def fetch_flags(class_, day_from, day_to, limit, offset):
        p = wb.flags_params(class_, day_from, day_to)
        in_day = [r for r in FLAGS if _in_day(p, r)]
        hit = [r for r in in_day if not p["class"] or r.flag_class == p["class"]]
        classes = Counter(r.flag_class for r in in_day)
        return {"available": True, "flags": [wb.shape_flag_row(r) for r in hit[offset:offset + limit]],
                "classes": dict(sorted(classes.items())), "total": len(hit), "limit": limit, "offset": offset}

    def fetch_search(q, limit):
        # the SEARCH_*_QUERY predicates: airline/airport name contains, everything else prefix-matches
        p = wb.search_params(q)
        low = p["q"].lower()
        hit = {
            "airlines": [r for r in search["airlines"] if low in r[0].lower()],
            "services": [r for r in search["services"] if r[0].upper().startswith(p["svc_q"])],
            "airframes": [r for r in search["airframes"]
                          if r[0].startswith(p["hex_q"]) or r[1].upper().replace("-", "").startswith(p["reg_q"])],
            "airports": [r for r in search["airports"]
                         if r[0].startswith(p["code_q"]) or r[1].startswith(p["code_q"])
                         or low in r[2].lower() or low in r[3].lower()],
        }
        shape = {"airlines": wb.shape_search_airline, "services": wb.shape_search_service,
                 "airframes": wb.shape_search_airframe, "airports": wb.shape_search_airport}
        return {k: [shape[k](r) for r in v[:limit]] for k, v in hit.items()}

    for name, fn in {"fetch_airlines": fetch_airlines, "fetch_services": fetch_services,
                     "fetch_instances": fetch_instances, "fetch_trends": fetch_trends, "fetch_flags": fetch_flags,
                     "fetch_search": fetch_search, "fetch_summary": lambda _f, _t: summary,
                     "fetch_estimates": lambda _f, _t: estimates, "fetch_coverage": lambda _f, _t: coverage}.items():
        setattr(store, name, fn)

    # /path is stubbed at the CH-fetch layer so the app's own status/cache/head classification runs for
    # real: pathed tiers get the trajectory, provisional/none flights fetch empty and classify by the head
    def fetch_path_auth(fid):
        r = _BY_ID.get(fid)
        return r and (r.icao24, r.callsign, False, _utc(r.start_time), _utc(r.end_time), r.start_time.date())

    app_mod._fetch_path_auth = fetch_path_auth
    app_mod._fetch_path_rich = lambda fid: PATH if fid in PATHED else []
    app_mod._fetch_path_head = lambda: HEAD
    app_mod._fetch_provisional = lambda *_a: []


# the ANA1 airframe parked live on the map's initial center, so a spec can pick it by clicking the canvas middle
_A = INSTANCES[0]
LIVE = {"hex": _A.icao24, "flight": _A.callsign, "registration": _A.registration, "typecode": _A.typecode,
        "lat": 35.69, "lon": 139.69, "alt_baro": "35000", "gs": 450.0, "track": 90.0}
SIGHTING = {"src": "rooftop", "ts": _utc(_A.start_time), "callsign": _A.callsign, "flight_id": FID,
            "origin": {"code": _A.o_iata, "name": _A.o_city}, "dest": {"code": _A.d_iata, "name": _A.d_city}}


def silence_live(app_mod):
    # no RisingWave/ClickHouse here: the poller ticks over a one-plane snapshot and LADD stays unloaded
    app_mod._fetch = lambda: {"server_ts": time.time(), "aircraft": [{**LIVE, "capture_ts": time.time()}]}
    app_mod._fetch_flights = lambda _hex: [SIGHTING]
    app_mod._fetch_outline = list
    app_mod._fetch_routes = dict
    app_mod._should_refresh_ladd = lambda *_a: False


if __name__ == "__main__":
    public = len(sys.argv) > 2 and sys.argv[2] == "public"
    mod = load_livemap_module("app.py", env=PUBLIC_ENV if public else None)
    silence_live(mod)
    if not public:
        install(mod)
    uvicorn.run(mod.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
