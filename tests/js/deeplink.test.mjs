import { test } from "node:test";
import assert from "node:assert/strict";
import { W, resolveDeepLink, jstDayOf } from "../../livemap/src/features/workbench/shell.js";
import { initFocus, enterFocus, dropFocus, focusedKey } from "../../livemap/src/features/workbench/focus.js";
import { stubFetch, spyFacade, flush } from "./_support.mjs";

const EPOCH = 1785421800;
const KEY = `86d3a1.${EPOCH}.ANA1`;
const ROW = {
  icao24: "86D3A1", start_ts: EPOCH, callsign: "ANA1", flight_id: "12345678901234000000", tier: "settled",
};
const DEFAULTS = { ...W }; // captured before any test moves state, so a new W field cannot leak between tests

// node has no DOM: stub only what url.js and the shell's status line touch
function setup(inst) {
  const historyCalls = [];
  globalThis.location = { search: `?wb=log&wb_inst=${inst}`, pathname: "/", hash: "" };
  globalThis.history = {
    state: null,
    replaceState(s, _t, u) { historyCalls.push(["replace", u]); this.state = s; },
    pushState(s, _t, u) { historyCalls.push(["push", u]); this.state = s; },
  };
  const fetches = stubFetch();
  dropFocus();
  const api = spyFacade();
  initFocus(api);
  Object.assign(W, DEFAULTS, { view: "log", inst, status: { textContent: "" } });
  const { calls } = api;
  return { fetches, historyCalls, calls };
}

test("a malformed key is dropped without a lookup", async () => {
  const { fetches } = setup("nonsense");
  await resolveDeepLink();
  assert.equal(W.inst, null);
  assert.deepEqual(fetches, []);
});

test("an unavailable lookup keeps the URL and says so", async () => {
  const { fetches } = setup(KEY);
  const p = resolveDeepLink();
  assert.match(fetches[0].url, /^\/workbench\/instances\?/);
  fetches[0].notOk();
  await p;
  assert.equal(W.inst, KEY); // superseded/failed is not not-found
  assert.equal(W.status.textContent, "deep link: lookup unavailable");
});

test("a resolve whose instance changed under it does nothing", async () => {
  const { fetches, calls } = setup(KEY);
  const p = resolveDeepLink();
  W.inst = "40aa11.1785425000.JAL2"; // a newer navigation owns the state
  fetches[0].ok({ instances: [ROW] });
  await p;
  assert.deepEqual(calls, []);
  assert.equal(W.status.textContent, "");
});

test("a hit focuses the flight and canonicalizes a shifted key", async () => {
  const shifted = `86d3a1.${EPOCH + 5}.ANA1`;
  const { fetches, historyCalls, calls } = setup(shifted);
  const p = resolveDeepLink();
  fetches[0].ok({ instances: [ROW] });
  await p;
  assert.equal(W.inst, KEY);
  assert.deepEqual(historyCalls, [["replace", `/?wb=log&wb_inst=${KEY}&wb_d=30d`]]);
  assert.deepEqual(calls, ["clearSelection", "dimLive:0.85", "guard:true", `show:${ROW.flight_id}:true`]);
  assert.equal(focusedKey(), KEY);
});

test("no candidate falls back to that airframe's day", async () => {
  const { fetches, historyCalls } = setup(KEY);
  const p = resolveDeepLink();
  fetches[0].ok({ instances: [] });
  await p;
  const day = jstDayOf(EPOCH);
  assert.equal(W.inst, null);
  assert.equal(W.view, "drill");
  assert.equal(W.hex, "86d3a1");
  assert.equal(W.range, `${day}..${day}`);
  assert.deepEqual(historyCalls, [["replace", `/?wb=drill&wb_d=${day}..${day}`]]);
  assert.equal(W.status.textContent, "instance not found — showing that day's flights for the airframe");
});

test("another flight already in focus is dropped silently before the lookup", async () => {
  const { calls } = setup(KEY);
  let exits = 0;
  enterFocus({ key: "40aa11.1785425000.JAL2", flightId: "777", tier: "settled" }, { onExit: () => exits++ });
  calls.length = 0;
  resolveDeepLink();
  await flush();
  assert.deepEqual(calls, ["clearPath", "dimLive:0", "guard:false"]);
  assert.equal(exits, 0);
  assert.equal(focusedKey(), null);
});
