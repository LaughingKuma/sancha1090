import { test } from "node:test";
import assert from "node:assert/strict";
import { readUrl, writeUrl } from "../../livemap/src/features/workbench/url.js";

// node has no DOM globals — stub the minimum readUrl/writeUrl touch, fresh per test.
function setLoc(search = "", pathname = "/app", hash = "") {
  globalThis.location = { search, pathname, hash };
}
function setHistory(initialState = null) {
  const calls = [];
  globalThis.history = {
    state: initialState,
    replaceState(s, _t, u) {
      calls.push(["replace", s, u]);
      this.state = s;
    },
    pushState(s, _t, u) {
      calls.push(["push", s, u]);
      this.state = s;
    },
  };
  return calls;
}

test("readUrl defaults on an empty query with no history state", () => {
  setLoc("");
  setHistory(null);
  const st = readUrl();
  assert.deepEqual(st, {
    view: "overview",
    range: "30d",
    airline: null,
    service: null,
    od: null,
    inst: null,
    mil: false,
    flagClass: null,
    dim: "route",
    page: 1,
    hex: null,
    apt: null,
    type: null,
  });
});

test("round-trip of every wb_* param", () => {
  setLoc("");
  const calls = setHistory(null);
  const st = {
    view: "drill",
    range: "7d",
    airline: "ANA",
    service: "ANA123",
    od: "HND-CTS",
    inst: "ABC123.123456789.ana123", // mixed case in: instNorm must lower the hex, upper the callsign
    mil: true,
    flagClass: "diversion",
    dim: "airline",
    page: 5,
    hex: "abcdef",
    apt: "hnd",
    type: "b738",
  };
  writeUrl(st);
  assert.equal(calls.length, 1);
  const [kind, pushedState, url] = calls[0];
  assert.equal(kind, "push");

  // scope rides history.state, not the query string
  assert.deepEqual(pushedState, { wb: { hex: "abcdef", apt: "hnd", type: "b738" } });
  const search = url.split("?")[1] ? `?${url.split("?")[1]}` : "";
  setLoc(search);
  setHistory(pushedState);
  const back = readUrl();
  assert.equal(back.view, "drill");
  assert.equal(back.range, "7d");
  assert.equal(back.airline, "ANA");
  assert.equal(back.service, "ANA123");
  assert.equal(back.od, "HND-CTS");
  assert.equal(back.inst, "abc123.123456789.ANA123");
  assert.equal(back.mil, true);
  assert.equal(back.flagClass, "diversion");
  assert.equal(back.dim, "airline");
  assert.equal(back.page, 5);
  assert.equal(back.hex, "abcdef");
  assert.equal(back.apt, "hnd");
  assert.equal(back.type, "b738");
});

test("VIEWS accepts the slice-3 views, and still rejects an unknown one", () => {
  for (const v of ["estimates", "coverage"]) {
    setLoc(`?wb=${v}`);
    setHistory(null);
    assert.equal(readUrl().view, v);

    setLoc("");
    const calls = setHistory(null);
    writeUrl({ view: v, range: "30d", airline: null, service: null, od: null, inst: null,
      mil: false, flagClass: null, dim: "route", page: 1, hex: null, apt: null, type: null });
    assert.ok(calls[0][2].includes(`wb=${v}`), `wb=${v} must survive the write`);
  }
  setLoc("?wb=not_a_view");
  setHistory(null);
  assert.equal(readUrl().view, "overview");
});

test("wb_class: valid class round-trips, invalid class reads as null", () => {
  setLoc("?wb_class=diversion");
  setHistory(null);
  assert.equal(readUrl().flagClass, "diversion");

  setLoc("?wb_class=same_endpoint");
  setHistory(null);
  assert.equal(readUrl().flagClass, "same_endpoint");

  setLoc("?wb_class=not_a_class");
  setHistory(null);
  assert.equal(readUrl().flagClass, null);
});

test("wb_dim: route is elided from the URL, non-route round-trips, invalid falls back to route", () => {
  setLoc("");
  let calls = setHistory(null);
  writeUrl({ view: "trends", range: "30d", airline: null, service: null, od: null, inst: null,
    mil: false, flagClass: null, dim: "route", page: 1, hex: null, apt: null, type: null });
  const url1 = calls[0][2];
  assert.ok(!url1.includes("wb_dim"), `route dim must be elided, got ${url1}`);

  calls = setHistory(null);
  writeUrl({ view: "trends", range: "30d", airline: null, service: null, od: null, inst: null,
    mil: false, flagClass: null, dim: "airline", page: 1, hex: null, apt: null, type: null });
  const url2 = calls[0][2];
  assert.ok(url2.includes("wb_dim=airline"));
  setLoc(`?${url2.split("?")[1]}`);
  setHistory(null);
  assert.equal(readUrl().dim, "airline");

  setLoc("?wb_dim=bogus");
  setHistory(null);
  assert.equal(readUrl().dim, "route");

  setLoc("");
  setHistory(null);
  assert.equal(readUrl().dim, "route");
});

test("instNorm canonicalization via wb_inst: hex lower, callsign UPPER", () => {
  setLoc("?wb_inst=ABCDEF.123456789.ke123");
  setHistory(null);
  assert.equal(readUrl().inst, "abcdef.123456789.KE123");
});

test("INST_RE accept/reject: legacy 2-part, 3-part, garbage", () => {
  setLoc("?wb_inst=abc123.123456789");
  setHistory(null);
  assert.equal(readUrl().inst, "abc123.123456789"); // legacy 2-part, no callsign segment

  setLoc("?wb_inst=abc123.123456789.ke123");
  setHistory(null);
  assert.equal(readUrl().inst, "abc123.123456789.KE123"); // 3-part

  setLoc("?wb_inst=not-a-key");
  setHistory(null);
  assert.equal(readUrl().inst, null); // garbage

  setLoc("?wb_inst=abc123.1234"); // epoch too short (< 9 digits)
  setHistory(null);
  assert.equal(readUrl().inst, null);

  setLoc("?wb_inst=zzzzzz.123456789"); // 'z' is not hex
  setHistory(null);
  assert.equal(readUrl().inst, null);
});

test("page clamps to 1..999", () => {
  const cases = [
    ["", 1],
    ["0", 1], // "0" is falsy in JS so parseInt(...)||1 falls through to 1, not 0
    ["-5", 1],
    ["abc", 1],
    ["42", 42],
    ["5000", 999],
  ];
  for (const [wbP, expected] of cases) {
    setLoc(wbP ? `?wb_p=${wbP}` : "");
    setHistory(null);
    assert.equal(readUrl().page, expected, `wb_p=${wbP}`);
  }
});

test("writing the same URL/scope replaces rather than pushes", () => {
  setLoc("?wb=overview&wb_d=30d");
  const calls = setHistory({ wb: { hex: null, apt: null, type: null } });
  const st = readUrl();
  writeUrl(st);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "replace");
});

test("writing a different state pushes a new history entry", () => {
  setLoc("?wb=overview&wb_d=30d");
  const calls = setHistory({ wb: { hex: null, apt: null, type: null } });
  const st = readUrl();
  writeUrl({ ...st, view: "log" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "push");
});

test("scope (hex/apt/type) rides history.state, never the query string", () => {
  setLoc("");
  const calls = setHistory(null);
  const st = {
    view: "log", range: "30d", airline: null, service: null, od: null, inst: null,
    mil: false, flagClass: null, dim: "route", page: 1, hex: "aabbcc", apt: "hnd", type: "b738",
  };
  writeUrl(st);
  const [, pushedState, url] = calls[0];
  assert.ok(!url.includes("hex="), `scope leaked into the query string: ${url}`);
  assert.ok(!url.includes("apt="), `scope leaked into the query string: ${url}`);
  assert.ok(!url.includes("type="), `scope leaked into the query string: ${url}`);

  // a decoy query param must not override the state-carried scope
  setLoc(`${url.split("?")[1] ? `?${url.split("?")[1]}&hex=decoy` : "?hex=decoy"}`);
  setHistory(pushedState);
  const back = readUrl();
  assert.equal(back.hex, "aabbcc");
  assert.equal(back.apt, "hnd");
  assert.equal(back.type, "b738");
});
