import { beforeEach, describe, expect, it } from "vitest";
import { effect } from "@preact/signals";
import { spyFacade, flush } from "./_support.mjs";
import {
  dropFocus, enterFocus, exitFocus, focus, focusedKey, initStore, isFocused,
} from "../../livemap/src/features/workbench/store";

// the focus signal replaced the copy-on-notify mirror: a subscriber sees the same claims the old
// onFocusChange listener did (the first run carries the current value, which is not a notification)
let stop: (() => void) | null = null;

function fresh() {
  dropFocus();
  stop?.();
  const api = spyFacade();
  api.seen = [];
  initStore(api);
  let first = true;
  stop = effect(() => {
    const f = focus.value;
    if (first) return void (first = false);
    api.seen.push(f && { key: f.inst.key, n: f.n });
  });
  return api;
}

const INST = { key: "86d3a1.1785421800.ANA1", flightId: "999", callsign: "ANA1", tier: "settled" } as never;
const OTHER = { key: "40aa11.1785425000.JAL2", flightId: "777", callsign: "JAL2", tier: "settled" } as never;

beforeEach(() => {
  dropFocus();
});

describe("focus controller", () => {
  it("an instance with no path never reaches the map", () => {
    const api = fresh();
    const msgs: string[] = [];
    enterFocus({ key: "x", flightId: null } as never, { onEmpty: (m) => msgs.push(m) });
    expect(msgs).toEqual(["no recorded path"]);
    expect(api.calls).toEqual([]);
    expect(isFocused()).toBe(false);
  });

  it("the claim lands before the path does", () => {
    const api = fresh();
    enterFocus(INST, {});
    expect(api.calls).toEqual(["clearSelection", "dimLive:0.85", "guard:true", "show:999:true"]);
    expect(api.seen).toEqual([{ key: "86d3a1.1785421800.ANA1", n: null }]);
    expect(isFocused()).toBe(true);
    expect(focusedKey()).toBe("86d3a1.1785421800.ANA1");
  });

  it("a drawn path fills in the point count", async () => {
    const api = fresh();
    enterFocus(INST, {});
    api.answers[0]({ status: "ok", n: 1234 });
    await flush();
    expect(api.seen.at(-1)).toEqual({ key: "86d3a1.1785421800.ANA1", n: 1234 });
    expect(isFocused()).toBe(true);
  });

  it("an empty path tears the claim down and reports it, without an exit callback", async () => {
    const api = fresh();
    let exits = 0;
    const msgs: string[] = [];
    enterFocus(INST, { onExit: () => exits++, onEmpty: (m) => msgs.push(m) });
    api.answers[0]({ status: "empty", n: 0 });
    await flush();
    expect(api.calls.slice(4)).toEqual(["clearPath", "dimLive:0", "guard:false"]);
    expect(msgs).toEqual(["no recorded path"]);
    expect(exits).toBe(0);
    expect(isFocused()).toBe(false);
    expect(focusedKey()).toBeNull();
    expect(api.seen.at(-1)).toBeNull();
  });

  it("an unreachable path says so", async () => {
    const api = fresh();
    const msgs: string[] = [];
    enterFocus(INST, { onEmpty: (m) => msgs.push(m) });
    api.answers[0]({ status: "failed", n: 0 });
    await flush();
    expect(msgs).toEqual(["path unavailable"]);
    expect(isFocused()).toBe(false);
  });

  it("a rejected path claim releases the guard instead of pinning the map", async () => {
    const api = fresh();
    const msgs: string[] = [];
    enterFocus(INST, { onEmpty: (m) => msgs.push(m) });
    api.rejects[0](new Error("facade bug"));
    await flush();
    expect(api.calls.slice(4)).toEqual(["clearPath", "dimLive:0", "guard:false"]);
    expect(msgs).toEqual(["path unavailable"]);
    expect(isFocused()).toBe(false);
  });

  it("a newer focus survives the older one's late answer", async () => {
    const api = fresh();
    enterFocus(INST, {});
    enterFocus(OTHER, {});
    api.answers[1]({ status: "ok", n: 42 });
    api.answers[0]({ status: "ok", n: 9 });
    await flush();
    expect(focusedKey()).toBe("40aa11.1785425000.JAL2");
    expect(api.seen.at(-1)).toEqual({ key: "40aa11.1785425000.JAL2", n: 42 });
  });

  it("a superseded answer leaves the claim exactly as it was", async () => {
    const api = fresh();
    enterFocus(INST, {});
    api.answers[0]({ status: "superseded", n: 0 });
    await flush();
    expect(focusedKey()).toBe("86d3a1.1785421800.ANA1");
    expect(api.seen).toEqual([{ key: "86d3a1.1785421800.ANA1", n: null }]);
  });

  it("exiting calls back once and ignores what arrives after", async () => {
    const api = fresh();
    let exits = 0;
    enterFocus(INST, { onExit: () => exits++ });
    exitFocus();
    exitFocus();
    expect(exits).toBe(1);
    api.answers[0]({ status: "ok", n: 5 });
    await flush();
    expect(isFocused()).toBe(false);
    expect(api.seen.at(-1)).toBeNull();
  });

  it("dropping a focus is silent — the incoming deep link's URL survives", () => {
    const api = fresh();
    let exits = 0;
    enterFocus(INST, { onExit: () => exits++ });
    dropFocus();
    expect(exits).toBe(0);
    expect(isFocused()).toBe(false);
    expect(api.calls.slice(4)).toEqual(["clearPath", "dimLive:0", "guard:false"]);
  });
});
