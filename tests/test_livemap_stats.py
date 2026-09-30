import asyncio
import datetime
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _livemap_loader import REPO_ROOT, load_livemap_module

try:
    import clickhouse_connect
    from clickhouse_connect.driver import exceptions as ch_exc
except ImportError:  # pragma: no cover - host env without the driver
    clickhouse_connect = None

LOADED_LADD = {"hex": frozenset(), "callsign": frozenset()}
LEAK = {"hex": "86d0e4", "icao24": "86d0e4", "registration": "JA123A", "callsign": "ANA123",
        "flight_id": "1234567890123", "lat": 35.1, "lon": 139.2, "r_dir": 123.0}
BANNED_KEYS = set(LEAK) | {"bad"}
D = datetime.date
ROOF_CUT = 1790661600
PATH_CUT = D(2026, 8, 28)


def _load(monkeypatch, public):
    if public:
        monkeypatch.setenv("LIVEMAP_PUBLIC_MODE", "1")
    else:
        monkeypatch.delenv("LIVEMAP_PUBLIC_MODE", raising=False)
    monkeypatch.setenv("LIVEMAP_LADD_CACHE_PATH", "/nonexistent/ladd_cache.json")
    return load_livemap_module("app.py", name=f"livemap_stats_{public}")


@pytest.fixture
def public_app(monkeypatch):
    return _load(monkeypatch, public=True)


@pytest.fixture
def private_app(monkeypatch):
    return _load(monkeypatch, public=False)


@pytest.fixture(params=[True, False], ids=["public", "private"])
def any_app(request, monkeypatch):
    app = _load(monkeypatch, public=request.param)
    monkeypatch.setattr(app, "_ladd_suppress", LOADED_LADD)
    return app


def _t(*dicts):
    cols = list(dicts[0])
    return cols, [tuple(d[c] for c in cols) for d in dicts]


def _utc_today(offset=0):
    return datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=offset)


def answers(s, n_open=70418):
    # Every row carries identity and position columns the assembly must never forward.
    return {
        s.EXCLUSION_QUERY: _t({"bad": ["abc123", "fedcba"], "n_open": n_open, "roof_cut": ROOF_CUT,
                               "path_cut": PATH_CUT, **LEAK}),
        s.TYPES_QUERY: _t(
            {"typecode": "B789", "model": "BOEING 787-9", "body_class": "widebody",
             "airframes": 5, "heli_airframes": 0, **LEAK},
            {"typecode": "H160", "model": None, "body_class": None,
             "airframes": 3, "heli_airframes": 2, **LEAK},
            {"typecode": "A320", "model": "AIRBUS A-320", "body_class": "narrowbody",
             "airframes": 4, "heli_airframes": 2, **LEAK},
            {"typecode": "BALL", "model": None, "body_class": None,
             "airframes": 1, "heli_airframes": 0, **LEAK}),
        s.RECORDS_QUERY: _t({"airframes": 13, "military": 2, "since": D(2026, 5, 23), "highest_ft": 54200.0,
                             "alt_tc": "BALL", "fastest_kt": 642.66, "gs_tc": "B789", "farthest_nmi": 153.4,
                             **LEAK}),
        s.HOURLY_QUERY: _t({"day": D(2026, 5, 23), "hour": 0, "aircraft": 50, **LEAK},
                           {"day": D(2026, 5, 24), "hour": 0, "aircraft": 6, **LEAK},
                           {"day": D(2026, 5, 25), "hour": 0, "aircraft": 8, **LEAK},
                           {"day": D(2026, 5, 26), "hour": 5, "aircraft": 90, **LEAK}),
        s.PEAK_MINUTE_QUERY: _t({"at": 1781597160, "aircraft": 44, **LEAK}),
        s.AIRLINES_QUERY: _t({"name": "Tiny Air", "flights": 500, "airframes": 2, **LEAK},
                             {"name": "All Nippon Airways", "flights": 100, "airframes": 3, **LEAK}),
        s.ROUTES_QUERY: _t({"o": "HND", "o_city": "Tokyo", "d": "XXX", "d_city": "Nowhere",
                            "flights": 90, "airframes": 2, **LEAK},
                           {"o": "HND", "o_city": "Tokyo", "d": "CTS", "d_city": "Sapporo",
                            "flights": 7, "airframes": 3, **LEAK}),
        # the partial first and trailing days carry the biggest counts, so keeping either would show
        s.DAILY_FLIGHTS_QUERY: _t({"day": D(2026, 8, 25), "flights": 99, **LEAK},
                                  {"day": D(2026, 8, 26), "flights": 10, **LEAK},
                                  {"day": D(2026, 8, 27), "flights": 30, **LEAK},
                                  {"day": D(2026, 8, 28), "flights": 30, **LEAK},
                                  {"day": D(2026, 8, 29), "flights": 500, **LEAK}),
        s.PARTS_QUERY: _t({"database": "bronze", "table": "adsb_states", "rows": 100, "bytes": 1000,
                           "raw_bytes": 5000},
                          {"database": "gold_ch", "table": "x", "rows": 10, "bytes": 10, "raw_bytes": 20},
                          {"database": "system", "table": "y", "rows": 1, "bytes": 1, "raw_bytes": 1}),
        s.FRESHNESS_QUERY: _t({"lane": "swim", "latest": 1790661500},
                              {"lane": "rooftop", "latest": 1790661599},
                              {"lane": "opensky", "latest": None},
                              {"lane": "adsb.lol", "latest": 1790639999}),
        s.ROWS_PER_DAY_QUERY: _t({"lane": "rooftop", "day": _utc_today(-2), "rows": 30},
                                 {"lane": "rooftop", "day": _utc_today(-1), "rows": 10}),
        s.MESSAGES_QUERY: _t({"day": D(2026, 9, 28), "messages": 3490121}),
    }


class _Res:
    def __init__(self, cols, rows):
        self.column_names, self.result_rows = cols, rows


class FakeCH:
    def __init__(self, table, fail=()):
        self.table, self.fail, self.calls = table, dict.fromkeys(fail, True), []
        self.lock = threading.Lock()

    def __call__(self):
        return self

    def query(self, sql, parameters=None):
        with self.lock:
            self.calls.append((sql, parameters))
        if sql in self.fail:
            raise RuntimeError("Code: 60. UNKNOWN_TABLE dim.dim_ladd does not exist")
        return _Res(*self.table[sql])

    def close(self):
        pass

    def ran(self, sql):
        return sum(1 for q, _ in self.calls if q == sql)


def _get(app, fake):
    app._ch_client = fake
    r = TestClient(app.app).get("/stats-data")
    assert r.status_code == 200
    return r.json()


# ---- LADD, fail closed ----

def test_public_unloaded_ladd_set_blanks_rooftop_without_querying(public_app):
    fake = FakeCH(answers(public_app.stats))
    body = _get(public_app, fake)
    assert body["rooftop"] == {"status": "unavailable"}
    assert fake.ran(public_app.stats.EXCLUSION_QUERY) == 0
    assert body["database"]["status"] == "ok"


def test_private_unloaded_ladd_set_still_serves_filtered_numbers(private_app):
    # The live set is a public-only gate; the page's own exclusion list is what filters both instances.
    assert private_app._ladd_suppress is None
    fake = FakeCH(answers(private_app.stats))
    body = _get(private_app, fake)
    assert body["rooftop"]["status"] == "ok"
    # once before the counting queries, once after to catch a rebuild mid-fill
    assert fake.ran(private_app.stats.EXCLUSION_QUERY) == 2


def test_exclusion_list_growing_mid_fill_blanks_rooftop(any_app):
    s = any_app.stats
    fake = FakeCH(answers(s))
    first, grown = fake.table[s.EXCLUSION_QUERY], _t({"bad": ["abc123", "fedcba", "0a1b2c"], "n_open": 70418,
                                                      "roof_cut": ROOF_CUT, "path_cut": PATH_CUT})
    seen = []
    orig = fake.query

    def query(sql, parameters=None):
        if sql == s.EXCLUSION_QUERY:
            seen.append(sql)
            fake.table[s.EXCLUSION_QUERY] = first if len(seen) == 1 else grown
        return orig(sql, parameters)

    fake.query = query
    body = _get(any_app, fake)
    assert body["rooftop"] == {"status": "unavailable"}
    assert body["database"]["status"] == "ok"


def test_missing_dim_ladd_blanks_rooftop_on_both_instances(any_app):
    s = any_app.stats
    fake = FakeCH(answers(s), fail=[s.EXCLUSION_QUERY])
    body = _get(any_app, fake)
    assert body["rooftop"] == {"status": "unavailable"}
    assert not any(fake.ran(q) for q in s.ROOFTOP_QUERIES)
    assert body["database"]["status"] == "ok"


@pytest.mark.parametrize("n_open", [0, 999])
def test_truncated_dim_ladd_blanks_rooftop_and_runs_nothing_else(any_app, n_open):
    s = any_app.stats
    fake = FakeCH(answers(s, n_open=n_open))
    body = _get(any_app, fake)
    assert body["rooftop"] == {"status": "unavailable"}
    assert not any(fake.ran(q) for q in s.ROOFTOP_QUERIES)
    assert body["database"]["status"] == "ok"


def test_exactly_the_minimum_open_rows_passes(any_app):
    body = _get(any_app, FakeCH(answers(any_app.stats, n_open=any_app.stats.STATS_MIN_LADD_OPEN)))
    assert body["rooftop"]["status"] == "ok"


@pytest.mark.parametrize("idx", range(7))
def test_any_failing_rooftop_query_blanks_the_whole_section(any_app, idx):
    s = any_app.stats
    body = _get(any_app, FakeCH(answers(s), fail=[s.ROOFTOP_QUERIES[idx]]))
    assert body["rooftop"] == {"status": "unavailable"}
    assert body["database"]["status"] == "ok"


def test_database_failure_leaves_rooftop_ok(any_app):
    s = any_app.stats
    body = _get(any_app, FakeCH(answers(s), fail=[s.FRESHNESS_QUERY]))
    assert body["database"] == {"status": "unavailable"}
    assert body["rooftop"]["status"] == "ok"


# ---- SQL shape ----

def test_exclusion_query_has_all_four_arms(private_app):
    x1 = private_app.stats.EXCLUSION_QUERY
    assert "flag_ladd = 1" in x1
    assert "ifNull(r.is_ladd, 1) = 1" in x1
    assert re.search(r"lower\(trimBoth\(icao24\)\) FROM dim\.dim_ladd FINAL\s+WHERE valid_to IS NULL", x1)
    assert re.search(r"upper\(trimBoth\(callsign\)\) FROM dim\.dim_ladd FINAL\s+WHERE valid_to IS NULL", x1)
    assert "ARRAY JOIN callsigns AS c" in x1 and "c IN ladd_cs" in x1
    assert "hex IN ladd_hex" in x1 and "lower(r.icao24) IN ladd_hex" in x1
    assert "upper(trimBoth(ifNull(r.callsign, ''))) IN ladd_cs" in x1


def test_every_counting_query_takes_the_bound_exclusion_list(private_app):
    s = private_app.stats
    for q in s.ROOFTOP_QUERIES:
        assert "NOT IN {bad:Array(String)}" in q
        assert "dim_ladd" not in q
        if "fct_flights_reconciled" in q:
            assert "r.icao24 IS NOT NULL AND lower(r.icao24) NOT IN {bad:Array(String)}" in q


def test_exclusion_list_is_bound_never_interpolated(private_app):
    s = private_app.stats
    fake = FakeCH(answers(s))
    _get(private_app, fake)
    for q in s.ROOFTOP_QUERIES:
        params = [p for sql, p in fake.calls if sql == q]
        assert params == [{"bad": ["abc123", "fedcba"], "roof_cut": ROOF_CUT, "path_cut": PATH_CUT}]
    assert not any("abc123" in sql for sql, _ in fake.calls)


def test_type_join_reads_empty_as_null(private_app):
    q = private_app.stats.TYPES_QUERY
    assert "nullIf(ty.model_name, '')" in q and "nullIf(ty.body_class, '')" in q


def test_duplicate_type_seed_row_cannot_inflate_a_type_past_the_floor(private_app):
    q = private_app.stats.TYPES_QUERY
    assert "uniqExact(a.hex) AS airframes" in q and "uniqExactIf(a.hex, a.heli = 1) AS heli_airframes" in q
    assert "LEFT ANY JOIN silver_ch.dim_aircraft_types" in q
    assert "count()" not in q


def test_every_rooftop_query_is_cut_where_the_exclusion_list_read(private_app):
    s = private_app.stats
    x1 = s.EXCLUSION_QUERY
    assert "toUnixTimestamp(max(hour_utc)) FROM gold_ch.agg_rooftop_airframe_hour) AS roof_cut" in x1
    assert "max(day_key) FROM gold_ch.fct_flight_path_summary) AS path_cut" in x1
    for q in s.ROOFTOP_QUERIES:
        rollup = q.count("gold_ch.agg_rooftop_airframe_hour")
        assert q.count("hour_utc < toDateTime({roof_cut:UInt32}, 'UTC')") == rollup, q
        summary = q.count("gold_ch.fct_flight_path_summary")
        assert q.count("day_key <= {path_cut:Date}") == summary, q
        assert summary == q.count("gold_ch.fct_flights_reconciled"), q
        assert rollup + summary >= 1, q


@pytest.mark.parametrize("missing", ["roof_cut", "path_cut"])
def test_exclusion_row_without_a_cut_blanks_rooftop(any_app, missing):
    s = any_app.stats
    table = answers(s)
    cols, (row,) = table[s.EXCLUSION_QUERY]
    row = tuple(None if c == missing else v for c, v in zip(cols, row, strict=True))
    table[s.EXCLUSION_QUERY] = (cols, [row])
    fake = FakeCH(table)
    assert _get(any_app, fake)["rooftop"] == {"status": "unavailable"}
    assert not any(fake.ran(q) for q in s.ROOFTOP_QUERIES)


def test_public_and_private_load_identical_queries(public_app, private_app):
    for name in ("EXCLUSION_QUERY", "ROOFTOP_QUERIES", "DATABASE_QUERIES"):
        assert getattr(public_app.stats, name) == getattr(private_app.stats, name)


def test_rollup_model_carries_the_edge_ladd_bit():
    model = REPO_ROOT / "dbt" / "sancha1090" / "models" / "marts" / "agg_rooftop_airframe_hour.sql"
    sql = model.read_text()
    assert "bitAnd(db_flags, 8)" in sql


# ---- no coordinates, no identities ----

COORD_WORDS = re.compile(r"\b(lat|lon|r_dir|FEEDER\w*)\b", re.IGNORECASE)


def test_coordinate_scan_is_whole_word():
    assert COORD_WORDS.search("SELECT lat FROM t")
    assert not COORD_WORDS.search("SELECT latest, relation, longest FROM t")


def test_no_stats_query_reads_a_coordinate(private_app):
    s = private_app.stats
    for q in (s.EXCLUSION_QUERY, *s.ROOFTOP_QUERIES, *s.DATABASE_QUERIES):
        assert not COORD_WORDS.search(q), q


def _walk(node, keys, strings):
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(k)
            _walk(v, keys, strings)
    elif isinstance(node, list):
        for v in node:
            _walk(v, keys, strings)
    elif isinstance(node, str):
        strings.append(node)


def test_json_carries_no_identity_or_position(any_app):
    body = _get(any_app, FakeCH(answers(any_app.stats)))
    keys, strings = set(), []
    _walk(body, keys, strings)
    assert not keys & BANNED_KEYS
    assert not [v for v in strings if re.fullmatch(r"[0-9a-fA-F]{6}", v)]
    text = json.dumps(body)
    for v in ("86d0e4", "JA123A", "ANA123", "1234567890123", "abc123", "fedcba", "35.1", "139.2"):
        assert v not in text


# ---- floor and assembly ----

def test_floor_drops_types_airlines_routes_below_three_airframes(private_app):
    roof = _get(private_app, FakeCH(answers(private_app.stats)))["rooftop"]
    assert [t["typecode"] for t in roof["types"]] == ["B789", "A320", "H160"]
    assert [t["typecode"] for t in roof["rarest_types"]] == ["H160", "A320", "B789"]
    assert roof["types_below_floor"] == 1
    assert [a["name"] for a in roof["airlines"]] == ["All Nippon Airways"]
    assert [(r["o"], r["d"]) for r in roof["routes"]] == [("HND", "CTS")]


def test_record_type_withheld_below_floor(private_app):
    rec = _get(private_app, FakeCH(answers(private_app.stats)))["rooftop"]["records"]
    assert rec == {"highest_ft": {"value": 54200, "typecode": None},
                   "fastest_kt": {"value": 642.7, "typecode": "B789"},
                   "farthest_nmi": 153, "farthest_km": 284}


def test_is_helicopter_needs_more_than_half_the_airframes(private_app):
    roof = _get(private_app, FakeCH(answers(private_app.stats)))["rooftop"]
    types = {t["typecode"]: t for t in roof["types"]}
    assert types["H160"]["is_helicopter"] is True
    assert types["A320"]["is_helicopter"] is False   # exactly half
    assert types["B789"]["is_helicopter"] is False


def test_rooftop_numbers_assemble(private_app):
    roof = _get(private_app, FakeCH(answers(private_app.stats)))["rooftop"]
    assert roof["since"] == "2026-05-23" and roof["airframes"] == 13 and roof["military_airframes"] == 2
    # partial first and trailing days are dropped before the busiest day and routes_as_of read them
    assert roof["per_day"] == [{"day": "2026-08-26", "flights": 10}, {"day": "2026-08-27", "flights": 30},
                               {"day": "2026-08-28", "flights": 30}]
    assert roof["busiest_day"] == {"day": "2026-08-27", "flights": 30}
    assert roof["routes_as_of"] == "2026-08-28"
    assert roof["peak_minute"] == {"at": 1781597160, "aircraft": 44}
    # partial first and last days are left out of the average
    hod = {h["hour"]: h["avg_aircraft"] for h in roof["hour_of_day"]}
    assert len(hod) == 24 and hod[0] == 7.0 and hod[5] == 0.0


def test_database_section_assembles(private_app):
    db = _get(private_app, FakeCH(answers(private_app.stats)))["database"]
    assert [x["layer"] for x in db["layers"]] == ["bronze", "gold"]
    assert db["layers"][0] == {"layer": "bronze", "rows": 100, "bytes": 1000, "raw_bytes": 5000, "ratio": 5.0}
    assert [f["lane"] for f in db["freshness"]] == ["rooftop", "opensky", "adsb.lol", "swim"]
    assert db["freshness"][1]["latest"] is None
    roof = db["per_day"][0]
    assert roof["lane"] == "rooftop" and roof["avg_30d"] == 20 and roof["bytes_per_day"] == 200
    assert roof["peak_30d"]["rows"] == 30


# ---- cache, single flight, staleness ----

def test_second_call_inside_ttl_makes_no_query(any_app):
    fake = FakeCH(answers(any_app.stats))
    _get(any_app, fake)
    n = len(fake.calls)
    _get(any_app, fake)
    assert len(fake.calls) == n


def test_failed_compute_is_not_cached(any_app):
    s = any_app.stats
    assert _get(any_app, FakeCH(answers(s), fail=[s.TYPES_QUERY]))["rooftop"]["status"] == "unavailable"
    fake = FakeCH(answers(s))
    assert _get(any_app, fake)["rooftop"]["status"] == "ok"
    assert fake.ran(s.EXCLUSION_QUERY) == 2  # one fresh fill: the list read before and after the counts


def test_rooftop_never_served_past_ttl_database_stale_up_to_an_hour(any_app):
    s = any_app.stats
    now = [1_000_000.0]
    any_app._stats_cache.mono = lambda: now[0]
    assert _get(any_app, FakeCH(answers(s)))["rooftop"]["status"] == "ok"
    down = FakeCH(answers(s), fail=list(answers(s)))
    now[0] += s.FRESH_TTL_S - 1
    body = _get(any_app, down)
    assert body["rooftop"]["status"] == "ok" and "stale" not in body["database"] and not down.calls
    now[0] += 2
    body = _get(any_app, down)
    assert body["rooftop"] == {"status": "unavailable"}
    assert body["database"]["status"] == "ok" and body["database"]["stale"] is True
    now[0] = 1_000_000.0 + s.DATABASE_STALE_MAX_S + 1
    assert _get(any_app, down)["database"] == {"status": "unavailable"}


def test_wall_clock_step_back_does_not_extend_freshness(any_app):
    s = any_app.stats
    wall, mono = [2_000_000.0], [500.0]
    any_app._stats_cache.clock = lambda: wall[0]
    any_app._stats_cache.mono = lambda: mono[0]
    assert _get(any_app, FakeCH(answers(s)))["rooftop"]["as_of"] == 2_000_000
    down = FakeCH(answers(s), fail=list(answers(s)))
    wall[0] -= 3 * s.DATABASE_STALE_MAX_S
    mono[0] += s.FRESH_TTL_S + 1
    body = _get(any_app, down)
    assert body["rooftop"] == {"status": "unavailable"}
    assert body["database"]["stale"] is True and body["database"]["as_of"] == 2_000_000
    mono[0] = 500.0 + s.DATABASE_STALE_MAX_S + 1
    assert _get(any_app, down)["database"] == {"status": "unavailable"}


def test_concurrent_cold_requests_run_one_compute(private_app):
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.1)
        return {"n": 1}

    async def burst():
        c = private_app.stats.StatsCache(private_app.cache.put)
        return await asyncio.gather(*(c.section("rooftop", compute) for _ in range(5)))

    out = asyncio.run(burst())
    assert len(calls) == 1
    assert all(o["status"] == "ok" and o["n"] == 1 for o in out)


def test_concurrent_cold_requests_share_one_failure(private_app):
    calls = []

    def compute():
        calls.append(1)
        time.sleep(0.1)
        raise RuntimeError("down")

    async def burst():
        c = private_app.stats.StatsCache(private_app.cache.put)
        return await asyncio.gather(*(c.section("rooftop", compute) for _ in range(5)))

    assert asyncio.run(burst()) == [{"status": "unavailable"}] * 5
    assert len(calls) == 1


# ---- public hardening ----

def test_public_stats_rate_limited_and_429_not_cacheable(public_app, monkeypatch):
    monkeypatch.setattr(public_app, "_ladd_suppress", LOADED_LADD)
    public_app._ch_client = FakeCH(answers(public_app.stats))
    client = TestClient(public_app.app)
    resps = [client.get("/stats-data") for _ in range(11)]
    assert all(r.status_code == 200 for r in resps[:10])
    assert all(r.headers["Cache-Control"] == "private, max-age=60" for r in resps[:10])
    assert resps[10].status_code == 429
    assert resps[10].json() == {"detail": "rate limited"}
    assert "public" not in resps[10].headers.get("Cache-Control", "")


def test_public_stats_max_age_never_outlives_the_rooftop_entry(public_app, monkeypatch):
    s = public_app.stats
    monkeypatch.setattr(public_app, "_ladd_suppress", LOADED_LADD)
    mono = [100.0]
    public_app._stats_cache.mono = lambda: mono[0]
    client = TestClient(public_app.app)
    public_app._ch_client = FakeCH(answers(s))
    assert client.get("/stats-data").headers["Cache-Control"] == "private, max-age=60"
    mono[0] += s.FRESH_TTL_S - 10.5
    assert client.get("/stats-data").headers["Cache-Control"] == "private, max-age=10"
    public_app._ch_client = FakeCH(answers(s), fail=list(answers(s)))
    mono[0] += 11
    r = client.get("/stats-data")
    assert r.json()["rooftop"] == {"status": "unavailable"}
    assert r.headers["Cache-Control"] == "private, max-age=0"


def test_private_stats_not_rate_limited(private_app):
    private_app._ch_client = FakeCH(answers(private_app.stats))
    client = TestClient(private_app.app)
    assert all(client.get("/stats-data").status_code == 200 for _ in range(15))


def test_public_features_still_404(public_app):
    assert TestClient(public_app.app).get("/features/workbench/").status_code == 404


@pytest.mark.parametrize("instance", ["public_app", "private_app"])
def test_stats_page_served_on_both_instances(instance, request):
    client = TestClient(request.getfixturevalue(instance).app)
    page = client.get("/stats/")
    assert page.status_code == 200
    assert page.headers["Cache-Control"] == "no-cache"
    assert '<script type="module" src="stats.js?v=' in page.text
    for asset in ("stats.js", "view.js", "stats.css"):
        assert client.get(f"/stats/{asset}").status_code == 200, asset


# ---- live ClickHouse: the literal queries execute ----

def test_stats_queries_execute_on_live_clickhouse(private_app):
    if clickhouse_connect is None:
        pytest.skip("clickhouse-connect unavailable")
    try:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
    except (ch_exc.OperationalError, OSError) as exc:
        # ONLY connectivity skips; a query ClickHouse rejects must fail here
        pytest.skip(f"live ClickHouse unreachable: {exc}")
    s = private_app.stats
    try:
        params = s.exclusion_params(s.rows(client, s.EXCLUSION_QUERY))
        roof = s.assemble_rooftop(*(s.rows(client, q, params) for q in s.ROOFTOP_QUERIES))
        db = s.assemble_database(*(s.rows(client, q) for q in s.DATABASE_QUERIES), _utc_today())
    finally:
        client.close()
    json.dumps({"rooftop": roof, "database": db})
    assert len(roof["hour_of_day"]) == 24
    assert [f["lane"] for f in db["freshness"]] == ["rooftop", "opensky", "adsb.lol", "swim"]


# ---- the wire the page's key list is checked against ----

STATS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "stats_payload.json"


def _fixture_payload(s):
    # Real assemblers over the canned rows; only the clock-relative parts are pinned so the file is stable.
    table = answers(s)
    table[s.ROWS_PER_DAY_QUERY] = _t({"lane": "rooftop", "day": D(2026, 9, 26), "rows": 30},
                                     {"lane": "rooftop", "day": D(2026, 9, 27), "rows": 10})
    fake = FakeCH(table)
    roof = s.assemble_rooftop(*(s.rows(fake, q) for q in s.ROOFTOP_QUERIES))
    db = s.assemble_database(*(s.rows(fake, q) for q in s.DATABASE_QUERIES), D(2026, 9, 29))
    ok = {"status": "ok", "as_of": 1790671188}
    return json.loads(json.dumps({
        "contract": s.STATS_CONTRACT, "generated_at": 1790671200, "floor": s.STATS_MIN_AIRFRAMES,
        "rooftop": {**ok, **roof}, "database": {**ok, **db}}))


def test_stats_payload_fixture_matches_server_output(private_app):
    payload = _fixture_payload(private_app.stats)
    # tests/ is a read-only mount in the container: write elsewhere, then docker cp it over the fixture
    if out := os.environ.get("REGEN_STATS_FIXTURE"):
        Path(out).write_text(json.dumps(payload, indent=2) + "\n")
    assert json.loads(STATS_FIXTURE.read_text()) == payload, (
        "stale fixture: rerun with REGEN_STATS_FIXTURE=/tmp/stats_payload.json and copy it to "
        "tests/fixtures/stats_payload.json, then run tests/js/stats_view.test.mjs")
