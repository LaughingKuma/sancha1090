import { test } from "node:test";
import assert from "node:assert/strict";
import {
  initFocus, enterFocus, exitFocus, dropFocus, isFocused, focusedKey, onFocusChange,
} from "../../livemap/src/features/workbench/focus.js";
import { spyFacade, flush } from "./_support.mjs";

// module state is reset by dropping any leftover focus first
function fresh() {
  dropFocus();
  const api = spyFacade();
  api.seen = [];
  initFocus(api);
  onFocusChange((f) => api.seen.push(f && { key: f.inst.key, n: f.n }));
  return api;
}

const INST = { key: "86d3a1.1785421800.ANA1", flightId: "999", callsign: "ANA1", tier: "settled" };
const OTHER = { key: "40aa11.1785425000.JAL2", flightId: "777", callsign: "JAL2", tier: "settled" };

test("an instance with no path never reaches the map", () => {
  const api = fresh();
  const msgs = [];
  enterFocus({ key: "x", flightId: null }, { onEmpty: (m) => msgs.push(m) });
  assert.deepEqual(msgs, ["no recorded path"]);
  assert.deepEqual(api.calls, []);
  assert.equal(isFocused(), false);
});

test("the claim lands before the path does", () => {
  const api = fresh();
  enterFocus(INST, {});
  assert.deepEqual(api.calls, ["clearSelection", "dimLive:0.85", "guard:true", "show:999:true"]);
  assert.deepEqual(api.seen, [{ key: INST.key, n: null }]);
  assert.equal(isFocused(), true);
  assert.equal(focusedKey(), INST.key);
});

test("a drawn path fills in the point count", async () => {
  const api = fresh();
  enterFocus(INST, {});
  api.answers[0]({ status: "ok", n: 1234 });
  await flush();
  assert.deepEqual(api.seen.at(-1), { key: INST.key, n: 1234 });
  assert.equal(isFocused(), true);
});

test("an empty path tears the claim down and reports it, without an exit callback", async () => {
  const api = fresh();
  let exits = 0;
  const msgs = [];
  enterFocus(INST, { onExit: () => exits++, onEmpty: (m) => msgs.push(m) });
  api.answers[0]({ status: "empty", n: 0 });
  await flush();
  assert.deepEqual(api.calls.slice(4), ["clearPath", "dimLive:0", "guard:false"]);
  assert.deepEqual(msgs, ["no recorded path"]);
  assert.equal(exits, 0);
  assert.equal(isFocused(), false);
  assert.equal(focusedKey(), null);
  assert.equal(api.seen.at(-1), null);
});

test("an unreachable path says so", async () => {
  const api = fresh();
  const msgs = [];
  enterFocus(INST, { onEmpty: (m) => msgs.push(m) });
  api.answers[0]({ status: "failed", n: 0 });
  await flush();
  assert.deepEqual(msgs, ["path unavailable"]);
  assert.equal(isFocused(), false);
});

test("a newer focus survives the older one's late answer", async () => {
  const api = fresh();
  enterFocus(INST, {});
  enterFocus(OTHER, {});
  api.answers[1]({ status: "ok", n: 42 });
  api.answers[0]({ status: "ok", n: 9 });
  await flush();
  assert.equal(focusedKey(), OTHER.key);
  assert.deepEqual(api.seen.at(-1), { key: OTHER.key, n: 42 });
});

test("a superseded answer leaves the claim exactly as it was", async () => {
  const api = fresh();
  enterFocus(INST, {});
  api.answers[0]({ status: "superseded", n: 0 });
  await flush();
  assert.equal(focusedKey(), INST.key);
  assert.deepEqual(api.seen, [{ key: INST.key, n: null }]);
});

test("exiting calls back once and ignores what arrives after", async () => {
  const api = fresh();
  let exits = 0;
  enterFocus(INST, { onExit: () => exits++ });
  exitFocus();
  exitFocus();
  assert.equal(exits, 1);
  api.answers[0]({ status: "ok", n: 5 });
  await flush();
  assert.equal(isFocused(), false);
  assert.equal(api.seen.at(-1), null);
});

test("dropping a focus is silent — the incoming deep link's URL survives", () => {
  const api = fresh();
  let exits = 0;
  enterFocus(INST, { onExit: () => exits++ });
  dropFocus();
  assert.equal(exits, 0);
  assert.equal(isFocused(), false);
  assert.deepEqual(api.calls.slice(4), ["clearPath", "dimLive:0", "guard:false"]);
});
