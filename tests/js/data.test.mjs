import { test } from "node:test";
import assert from "node:assert/strict";
import {
  fetchInstances,
  fetchSummary,
  fetchTrends,
  fetchFlags,
} from "../../livemap/static/features/workbench/data.js";

function mockFetch(payload, ok = true) {
  globalThis.fetch = async () => ({ ok, json: async () => payload });
}

test("fetchInstances: key minting incl. callsign segment and the no-callsign form, tier fallback, mil booleans", async () => {
  mockFetch({
    instances: [
      {
        icao24: "ABCDEF", day: "2026-07-10", start_ts: 1752122400, end_ts: 1752126000,
        callsign: "ANA123", airline: "All Nippon Airways", registration: "JA123A", typecode: "B738",
        origin: { icao: "RJTT", iata: "HND", city: "Tokyo" },
        dest: { icao: "RJCC", iata: "CTS", city: "Sapporo" },
        tier: "settled", effective_gap_s: 12, n_points: 340, is_military: false,
        flight_id: 123456789012345,
      },
      {
        icao24: "abcdef", day: "2026-07-10", start_ts: 1752123000, end_ts: null,
        callsign: "", airline: null, registration: null, typecode: null,
        origin: { icao: null, iata: null, city: null }, dest: { icao: null, iata: null, city: null },
        tier: "bogus-tier", effective_gap_s: null, n_points: null, is_military: 1, flight_id: null,
      },
    ],
    od_breakdown: [{ o: "HND", d: "CTS", n: 42 }],
    total: 2, limit: 50, offset: 0, military_filter_available: false,
  });
  const r = await fetchInstances({});
  assert.equal(r.rows.length, 2);

  const [a, b] = r.rows;
  assert.equal(a.key, "abcdef.1752122400.ANA123"); // callsign segment present
  assert.equal(a.tier, "settled");
  assert.equal(a.mil, false);
  assert.equal(a.flightId, "123456789012345");

  assert.equal(b.key, "abcdef.1752123000"); // no callsign -> no trailing segment
  assert.equal(b.tier, "unknown"); // unrecognized tier value falls back
  assert.equal(b.mil, true); // is_military: 1
  assert.equal(b.flightId, null);

  assert.deepEqual(r.od, [{ o: "HND", d: "CTS", n: 42 }]);
  assert.equal(r.total, 2);
  assert.equal(r.limit, 50);
  assert.equal(r.offset, 0);
  assert.equal(r.milAvailable, false); // explicit false disables the control
});

test("fetchInstances: milAvailable defaults true when the field is absent (absent-not-hidden)", async () => {
  mockFetch({ instances: [], od_breakdown: [], total: 0, limit: 50, offset: 0 });
  const r = await fetchInstances({});
  assert.equal(r.milAvailable, true);
});

test("fetchInstances: hostile-length fields are truncated to their caps", async () => {
  mockFetch({
    instances: [
      {
        icao24: "AABBCCDDEE", day: "2026-07-10-extra", start_ts: 1752122400, end_ts: 1752126000,
        callsign: "THISISAVERYLONGCALLSIGN", airline: "A".repeat(200),
        registration: "REG1234567890EXTRA", typecode: "TYPECODE123",
        origin: { icao: "ICAOTOOLONG12", iata: "IATA12345", city: "C".repeat(60) },
        dest: { icao: "RJCC", iata: "CTS", city: "Sapporo" },
        tier: "estimated", effective_gap_s: 5, n_points: 10, is_military: false,
        flight_id: "1".repeat(40),
      },
    ],
    od_breakdown: [], total: 1, limit: 50, offset: 0,
  });
  const r = await fetchInstances({});
  const row = r.rows[0];
  assert.equal(row.hex, "aabbcc"); // 6-char cap, then lowercased
  assert.equal(row.day, "2026-07-10"); // 10-char cap
  assert.equal(row.callsign, "THISISAVERYLONGC"); // 16-char cap
  assert.equal(row.airline, "A".repeat(128)); // NAME_MAX cap
  assert.equal(row.reg, "REG123456789"); // 12-char cap
  assert.equal(row.type, "TYPECODE"); // 8-char cap
  assert.equal(row.origin.icao, "ICAOTOOL"); // 8-char cap
  assert.equal(row.origin.iata, "IATA"); // 4-char cap
  assert.equal(row.origin.city, "C".repeat(48)); // 48-char cap
  assert.equal(row.flightId, "1".repeat(24)); // 24-char cap
  assert.equal(row.key, "aabbcc.1752122400.THISISAV"); // csKeyOf caps the raw callsign to 8, independently
});

test("fetchSummary: full envelope shape, invalid daily entries dropped", async () => {
  mockFetch({
    flights: 178553, aircraft: 6893, services: 10784,
    daily: [["2026-07-10", 5434], ["", 999], ["2026-07-11", "bad"], ["2026-07-11", 5200]],
    flags: {
      available: true, flagged: 22259,
      classes: {
        tiebreak_endpoint: 8400, single_source: 5660, one_sided_intl: 6921,
        feasibility_snap: 705, diversion: 0, military: 2014, unknown_class: 111,
      },
    },
    tiers: {
      available: true,
      mix: { settled: 101673, estimated: 68834, provisional: 8036, none: 0 },
      daily: [["2026-07-10", { settled: 3511, estimated: 1923 }]],
    },
    est: { available: true, err_p50_km: 0.57, n: 232, daily: [["2026-07-24", 4.47, 10]] },
    movers: [{ key: "GMP-CJU", n: 3072, prev_n: 2734, delta_pct: 12.4 }],
  });
  const r = await fetchSummary({});
  assert.equal(r.flights, 178553);
  assert.equal(r.aircraft, 6893);
  assert.equal(r.services, 10784);
  assert.deepEqual(r.daily, [["2026-07-10", 5434], ["2026-07-11", 5200]]);

  assert.equal(r.flags.available, true);
  assert.equal(r.flags.flagged, 22259);
  // classCounts keeps an explicit 0 (!= null check) but drops the unrecognized class key
  assert.deepEqual(r.flags.classes, {
    tiebreak_endpoint: 8400, single_source: 5660, one_sided_intl: 6921,
    feasibility_snap: 705, diversion: 0, military: 2014,
  });

  assert.equal(r.tiers.available, true);
  // tierCounts uses a truthy check — an explicit 0 tier is dropped, unlike classCounts
  assert.deepEqual(r.tiers.mix, { settled: 101673, estimated: 68834, provisional: 8036 });
  assert.deepEqual(r.tiers.daily, [["2026-07-10", { settled: 3511, estimated: 1923 }]]);

  assert.equal(r.est.available, true);
  assert.equal(r.est.errP50Km, 0.57);
  assert.equal(r.est.n, 232);
  assert.deepEqual(r.est.daily, [["2026-07-24", 4.47, 10]]);

  assert.deepEqual(r.movers, [{ key: "GMP-CJU", n: 3072, prevN: 2734, deltaPct: 12.4 }]);
});

test("fetchSummary: available:false sections normalize to the empty-envelope shapes", async () => {
  mockFetch({
    flights: 0, aircraft: 0, services: 0, daily: [],
    flags: { available: false, flagged: 0, classes: {} },
    tiers: { available: false, mix: {}, daily: [] },
    est: { available: false, err_p50_km: null, n: 0, daily: [] },
    movers: [],
  });
  const r = await fetchSummary({});
  assert.equal(r.flags.available, false);
  assert.equal(r.tiers.available, false);
  assert.equal(r.est.available, false);
  assert.equal(r.est.errP50Km, null);
});

test("fetchTrends: rank/series shape, key cap, null delta on no prior window", async () => {
  mockFetch({
    dim: "route", grain: "day",
    series: [
      { key: "GMP-CJU", points: [["2026-07-10", 98], ["2026-07-11", "x"]] },
      { key: "", points: [] },
    ],
    rank: [
      { key: "GMP-CJU", n: 3072, distinct_aircraft: 289, prev_n: 2734, delta_pct: 12.4 },
      { key: "A".repeat(200), n: 10, distinct_aircraft: 2, prev_n: 0, delta_pct: null },
    ],
    total: 493, limit: 20, offset: 0,
  });
  const r = await fetchTrends({ dim: "route" });
  assert.equal(r.dim, "route");
  assert.deepEqual(r.rank[0], { key: "GMP-CJU", n: 3072, distinctAircraft: 289, prevN: 2734, deltaPct: 12.4 });
  assert.deepEqual(r.rank[1], { key: "A".repeat(128), n: 10, distinctAircraft: 2, prevN: 0, deltaPct: null });
  assert.deepEqual(r.series[0], { key: "GMP-CJU", points: [["2026-07-10", 98]] }); // "x" is not a number, dropped
  assert.deepEqual(r.series[1], { key: "", points: [] });
  assert.equal(r.total, 493);
  assert.equal(r.limit, 20);
  assert.equal(r.offset, 0);
});

test("fetchFlags: row shape (instance() + flagClass/detail) and classes filtering", async () => {
  mockFetch({
    available: true,
    flags: [
      {
        icao24: "ABCDEF", day: "2026-07-28", start_ts: 1753700000, end_ts: 1753701000,
        callsign: "JAL123", airline: "Japan Airlines", registration: "JA1JAL", typecode: "B788",
        origin: { icao: "RJAA", iata: "NRT", city: "Narita" },
        dest: { icao: "RJGG", iata: "NGO", city: "Nagoya" },
        tier: "settled", effective_gap_s: 3, n_points: 120, is_military: false,
        flight_id: 99887766,
        flag_class: "diversion", detail: "dest RJNA vs modal RJGG 79/87",
      },
    ],
    classes: { diversion: 487, one_sided_intl: 6921, bogus: 5 },
    total: 486, limit: 50, offset: 0,
  });
  const r = await fetchFlags({});
  assert.equal(r.available, true);
  assert.equal(r.rows.length, 1);
  const row = r.rows[0];
  assert.equal(row.key, "abcdef.1753700000.JAL123");
  assert.equal(row.flagClass, "diversion");
  assert.equal(row.detail, "dest RJNA vs modal RJGG 79/87");
  assert.deepEqual(r.classes, { diversion: 487, one_sided_intl: 6921 }); // "bogus" dropped
  assert.equal(r.total, 486);
});

test("fetchFlags: available:false and hostile-length flag_class/detail truncation", async () => {
  mockFetch({
    available: false,
    flags: [
      {
        icao24: "abcdef", day: "2026-07-28", start_ts: 1753700000, end_ts: null,
        callsign: "X", airline: "Y", registration: "Z", typecode: "W",
        origin: { icao: null, iata: null, city: null }, dest: { icao: null, iata: null, city: null },
        tier: "none", effective_gap_s: null, n_points: null, is_military: false, flight_id: null,
        flag_class: "c".repeat(40), detail: "d".repeat(200),
      },
    ],
    classes: {}, total: 0, limit: 50, offset: 0,
  });
  const r = await fetchFlags({});
  assert.equal(r.available, false); // mart-not-deployed signal
  assert.equal(r.rows[0].flagClass, "c".repeat(24));
  assert.equal(r.rows[0].detail, "d".repeat(96));
});

test("seq-guard: overlapping fetchInstances calls — the earlier resolves null when superseded", async () => {
  let n = 0;
  globalThis.fetch = async () => {
    n += 1;
    const mine = n;
    if (mine === 1) await new Promise((r) => setTimeout(r, 30)); // first call resolves after the second
    return { ok: true, json: async () => ({ instances: [], od_breakdown: [], total: 0, limit: 50, offset: 0 }) };
  };
  const p1 = fetchInstances({});
  const p2 = fetchInstances({});
  const [r1, r2] = await Promise.all([p1, p2]);
  assert.equal(r1, null); // superseded while in flight
  assert.notEqual(r2, null);
});

test("!r.ok resolves null", async () => {
  mockFetch({ instances: [] }, false);
  assert.equal(await fetchInstances({}), null);
  assert.equal(await fetchSummary({}), null);
});

test("a json()-throwing response resolves null (caught, never a broken rail)", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => { throw new Error("bad json"); },
  });
  assert.equal(await fetchInstances({}), null);
  assert.equal(await fetchTrends({}), null);
});
