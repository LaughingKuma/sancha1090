import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import {
  UNAVAILABLE, ageSeconds, barGeometry, completeDays, fillDays, fits, fmtAgo, fmtBytes, fmtCount, fmtDay, fmtDec, fmtInt,
  fmtMinuteJst, niceScale, parseEnvelope, wedgePath,
} from "../../livemap/static/stats/view.js";

const staticDir = fileURLToPath(new URL("../../livemap/static/", import.meta.url));
const statsDir = join(staticDir, "stats");

const rooftop = () => ({
  status: "ok", as_of: 1790671188, since: "2026-05-23", airframes: 4787,
  types: [{ typecode: "B789", model: "BOEING 787-9", body_class: "widebody", is_helicopter: false, airframes: 427 }],
  rarest_types: [], types_below_floor: 45,
  airlines: [{ name: "All Nippon Airways", flights: 63361, airframes: 224 }],
  routes: [{ o: "HND", o_city: "Tokyo", d: "CTS", d_city: "Sapporo", flights: 7124 }], routes_as_of: "2026-09-27",
  hour_of_day: [{ hour: 0, avg_aircraft: 27.3 }], per_day: [{ day: "2026-08-27", flights: 2288 }],
  busiest_day: { day: "2026-08-27", flights: 2288 }, peak_minute: { at: 1781597160, aircraft: 44 },
  records: { highest_ft: { value: 54200, typecode: null }, fastest_kt: { value: 642.7, typecode: "GLEX" },
    farthest_nmi: 153, farthest_km: 283 },
  military_airframes: 287,
});
const database = () => ({
  status: "ok", stale: true, as_of: 1790671188,
  layers: [{ layer: "bronze", rows: 1016415892, bytes: 84430623228, raw_bytes: 484904036415, ratio: 5.7 }],
  freshness: [{ lane: "rooftop", latest: 1790668799 }],
  per_day: [{ lane: "rooftop", days: [{ day: "2026-05-23", rows: 180936 }], avg_30d: 668183,
    peak_30d: { day: "2026-09-14", rows: 754580 }, bytes_per_day: 41289445 }],
  messages_per_day: [{ day: "2026-08-30", messages: 3536193 }],
});
const envelope = (over = {}) => ({ contract: 1, generated_at: 1790671200, floor: 3, rooftop: rooftop(), database: database(), ...over });

test("a well-formed envelope passes both sections through untouched", () => {
  const env = envelope();
  const got = parseEnvelope(env);
  assert.equal(got.rooftop, env.rooftop);
  assert.equal(got.database, env.database);
});

test("an identity key anywhere in a section rejects that section only", () => {
  for (const leak of ["hex", "icao24", "registration", "callsign", "lat", "lon", "r_dir", "flight_id"]) {
    const r = rooftop();
    r.types[0][leak] = "x";
    assert.equal(parseEnvelope(envelope({ rooftop: r })).rooftop, UNAVAILABLE, `types[].${leak}`);
    const top = rooftop();
    top[leak] = "x";
    assert.equal(parseEnvelope(envelope({ rooftop: top })).rooftop, UNAVAILABLE, leak);
    const rec = rooftop();
    rec.records.fastest_kt[leak] = "x";
    const got = parseEnvelope(envelope({ rooftop: rec }));
    assert.equal(got.rooftop, UNAVAILABLE, `records.fastest_kt.${leak}`);
    assert.notEqual(got.database, UNAVAILABLE);
  }
  const db = database();
  db.freshness[0].hex = "abc123";
  assert.equal(parseEnvelope(envelope({ database: db })).database, UNAVAILABLE);
});

test("a wrong contract, a non-ok status or a bad body gives both sections unavailable", () => {
  assert.deepEqual(parseEnvelope(envelope({ contract: 2 })), { rooftop: UNAVAILABLE, database: UNAVAILABLE });
  assert.deepEqual(parseEnvelope(null), { rooftop: UNAVAILABLE, database: UNAVAILABLE });
  assert.deepEqual(parseEnvelope("x"), { rooftop: UNAVAILABLE, database: UNAVAILABLE });
  const got = parseEnvelope(envelope({ rooftop: { status: "unavailable" } }));
  assert.equal(got.rooftop, UNAVAILABLE);
  assert.notEqual(got.database, UNAVAILABLE);
});

test("the key check rejects an object where a list belongs and ignores prototype names", () => {
  assert.equal(fits({ types: { typecode: "B789" } }, { types: [{ typecode: 1 }] }), false);
  assert.equal(fits({ constructor: 1 }, { status: 1 }), false);
});

test("null passes only where the server sends it, and a leaf never holds an object", () => {
  const roof = rooftop();
  roof.busiest_day = null;
  roof.peak_minute = null;
  assert.equal(parseEnvelope(envelope({ rooftop: roof })).rooftop, roof);
  const db = database();
  db.per_day[0].peak_30d = null;
  assert.equal(parseEnvelope(envelope({ database: db })).database, db);
  const bad = [
    ["types", null], ["airlines", null], ["hour_of_day", null], ["per_day", null], ["records", null],
  ];
  for (const [key, v] of bad) {
    const r = rooftop();
    r[key] = v;
    assert.equal(parseEnvelope(envelope({ rooftop: r })).rooftop, UNAVAILABLE, `rooftop.${key} null`);
  }
  for (const key of ["layers", "freshness", "per_day", "messages_per_day"]) {
    const d = database();
    d[key] = null;
    assert.equal(parseEnvelope(envelope({ database: d })).database, UNAVAILABLE, `database.${key} null`);
  }
  const rec = rooftop();
  rec.records.highest_ft = null;
  assert.equal(parseEnvelope(envelope({ rooftop: rec })).rooftop, UNAVAILABLE, "records.highest_ft null");
  const leaf = rooftop();
  leaf.types[0].model = { hex: "abc123" };
  assert.equal(parseEnvelope(envelope({ rooftop: leaf })).rooftop, UNAVAILABLE, "object in a leaf");
  const nullLeaf = rooftop();
  nullLeaf.types[0].model = null;
  assert.equal(parseEnvelope(envelope({ rooftop: nullLeaf })).rooftop, nullLeaf);
});

// The page's key list is hand-kept, so the real server output is what proves it still accepts what is sent.
test("the payload the server code builds passes both sections", () => {
  const body = JSON.parse(readFileSync(join(statsDir, "../../../tests/fixtures/stats_payload.json"), "utf8"));
  const got = parseEnvelope(body);
  assert.equal(got.rooftop.status, "ok");
  assert.equal(got.database.status, "ok");
  assert.equal(got.rooftop, body.rooftop);
  assert.equal(got.database, body.database);
});

test("numbers format with grouping and short scales", () => {
  assert.equal(fmtInt(63361), "63,361");
  assert.equal(fmtInt(null), "—");
  assert.equal(fmtDec(642.7), "642.7");
  assert.equal(fmtDec(144), "144.0");
  assert.equal(fmtCount(1016415892), "1.02 billion");
  assert.equal(fmtCount(146070270), "146 million");
  assert.equal(fmtCount(999), "999");
  assert.equal(fmtBytes(84430623228), "84.4 GB");
  assert.equal(fmtBytes(25368635), "25.4 MB");
  assert.equal(fmtBytes(624474), "624 kB");
  assert.equal(fmtBytes(12), "12 B");
});

test("days and minutes print in Japan time without shifting a server day", () => {
  assert.equal(fmtDay("2026-08-27"), "27 Aug 2026");
  assert.equal(fmtDay("2026-08-27", { weekday: true }), "Thu 27 Aug 2026");
  assert.equal(fmtDay("2026-09-01", { year: false }), "1 Sep");
  assert.equal(fmtDay("junk"), "—");
  // 2026-06-16 08:06 UTC is 17:06 JST
  assert.equal(fmtMinuteJst(1781597160), "16 Jun 2026, 17:06 JST");
  assert.equal(fmtMinuteJst(Date.UTC(2026, 0, 31, 15, 30) / 1000), "1 Feb 2026, 00:30 JST");
});

test("ages read in plain words and never go negative", () => {
  assert.equal(fmtAgo(20), "just now");
  assert.equal(fmtAgo(5 * 60 + 10), "5 min ago");
  assert.equal(fmtAgo(66 * 60), "1 h 6 min ago");
  assert.equal(fmtAgo(8 * 3600 + 120), "8 h ago");
  assert.equal(fmtAgo(3 * 86400), "3 days ago");
  assert.equal(ageSeconds(1000, 900 * 1000), 0);
  assert.equal(ageSeconds(null, 0), null);
});

test("an axis tops out at a round number at or above the data", () => {
  assert.deepEqual(niceScale(2288, 4), { max: 3000, ticks: [0, 1000, 2000, 3000] });
  assert.deepEqual(niceScale(2288, 5), { max: 2500, ticks: [0, 500, 1000, 1500, 2000, 2500] });
  assert.deepEqual(niceScale(145.4, 3), { max: 150, ticks: [0, 50, 100, 150] });
  assert.deepEqual(niceScale(0, 4).ticks[0], 0);
  for (const m of [1, 7, 99, 4426652]) assert.ok(niceScale(m).max >= m);
});

test("bars fill their slots, scale to the max and sit on the baseline", () => {
  const bars = barGeometry([10, 5, 0, null], { width: 100, height: 50, gap: 2, max: 10 });
  assert.equal(bars.length, 4);
  assert.deepEqual(bars[0], { x: 0, y: 0, w: 23, h: 50 });
  assert.deepEqual(bars[1], { x: 25, y: 25, w: 23, h: 25 });
  assert.equal(bars[2].h, 0);
  assert.equal(bars[3].h, 0);
  assert.equal(barGeometry([15], { width: 10, height: 10, max: 10 })[0].h, 10);
  assert.deepEqual(barGeometry([], { width: 100, height: 50 }), []);
});

test("the dial's first wedge starts at the top and runs clockwise", () => {
  const d = wedgePath(0, 24, 10, 20, 50, 50, 0);
  assert.match(d, /^M50\.00 40\.00 L50\.00 30\.00 A20 20 0 0 1 /);
  const quarter = wedgePath(6, 24, 10, 20, 50, 50, 0);
  assert.match(quarter, /^M60\.00 50\.00 L70\.00 50\.00/);
});

test("today's still-filling UTC day is left off the warehouse charts", () => {
  const now = Date.UTC(2026, 8, 29, 6, 0);
  const rows = [{ day: "2026-09-27" }, { day: "2026-09-28" }, { day: "2026-09-29" }];
  assert.deepEqual(completeDays(rows, now).map((r) => r.day), ["2026-09-27", "2026-09-28"]);
  assert.deepEqual(completeDays(undefined, now), []);
});

test("a day with no row keeps its slot, so the time axis stays true", () => {
  const got = fillDays([{ day: "2026-08-30", flights: 5 }, { day: "2026-09-02", flights: 7 }], "flights");
  assert.deepEqual(got.map((r) => [r.day, r.flights]),
    [["2026-08-30", 5], ["2026-08-31", null], ["2026-09-01", null], ["2026-09-02", 7]]);
  assert.deepEqual(fillDays([], "flights"), []);
});

// The release sed and wb_gate_check's pin scan only see double-quoted "…?v=…" specifiers.
const statsFiles = readdirSync(statsDir).filter((f) => /\.(js|html|css)$/.test(f));

test("every ?v= under static/stats is double-quoted", () => {
  for (const f of statsFiles) {
    const text = readFileSync(join(statsDir, f), "utf8");
    assert.doesNotMatch(text, /'[^'\n]*\?v=[^'\n]*'/, `${f}: single-quoted ?v=`);
    assert.doesNotMatch(text, /`[^`]*\?v=[^`]*`/, `${f}: ?v= inside a template literal`);
  }
});

test("the stats page carries the map's one ?v= pin on every relative reference", () => {
  const mapPin = /"map\.js\?v=([^"]+)"/.exec(readFileSync(join(staticDir, "index.html"), "utf8"))[1];
  let seen = 0;
  for (const f of statsFiles) {
    const text = readFileSync(join(statsDir, f), "utf8");
    for (const m of text.matchAll(/(?:from\s*|src=|href=)"(\.{1,2}\/[^"]*|[\w-]+\.(?:js|css)[^"]*)"/g)) {
      if (m[1].startsWith("../") && !/\.(js|css)/.test(m[1])) continue; // a page link, not a module
      assert.match(m[1], new RegExp(`\\?v=${mapPin.replaceAll(".", "\\.")}$`), `${f}: ${m[1]}`);
      seen += 1;
    }
  }
  assert.ok(seen >= 4, `expected the css, the entry and two imports, saw ${seen}`);
});

test("the stats page builds DOM from text, never from HTML strings", () => {
  for (const f of statsFiles.filter((x) => x.endsWith(".js"))) {
    const text = readFileSync(join(statsDir, f), "utf8");
    assert.doesNotMatch(text, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/, f);
  }
});

test("the map never imports stats code, so its module graph is unchanged", () => {
  for (const f of readdirSync(staticDir).filter((x) => x.endsWith(".js"))) {
    assert.doesNotMatch(readFileSync(join(staticDir, f), "utf8"), /stats\//, f);
  }
  assert.match(readFileSync(join(staticDir, "index.html"), "utf8"), /<a class="stats-link" href="stats\/">/);
});

const css = readFileSync(join(statsDir, "stats.css"), "utf8");
const lightBlock = /prefers-color-scheme: light\) \{\s*:root \{([^}]*)\}/.exec(css)[1];
const lightVar = (name) => new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, "i").exec(lightBlock)[1];
const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

test("light-mode chart marks and the military figure reach 3:1 against the panel", () => {
  const panel = lightVar("panel");
  for (const name of ["bar", "bar-soft", "wedge", "mil"]) {
    assert.ok(contrast(lightVar(name), panel) >= 3, `--${name} ${contrast(lightVar(name), panel).toFixed(2)}:1`);
  }
});

test("the brand link's focus ring is not clipped away", () => {
  const rule = /\n\.brand \{([^}]*)\}/.exec(css)[1];
  assert.doesNotMatch(rule, /clip-path/);
  assert.match(css, /\.brand::before \{[^}]*clip-path/);
});
