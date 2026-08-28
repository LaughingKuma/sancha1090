import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EPOCH, KEY, flush, resetStore, stubFetch } from "./_fixtures";
import { readUrl, type WbState } from "../../livemap/src/features/workbench/url";
import {
  SCOPE_DEFAULTS, SCOPE_KEYS, VIEW_SCOPE, applyUrl, doorway, enterFocus, focus, focusedKey,
  jstDayOf, milAvailable, milUnavailable, navigate, openAirframe, openAirline, openAirport,
  openService, rangeParams, resolveDeepLink, status, wb,
} from "../../livemap/src/features/workbench/store";

const ROW = {
  icao24: "86D3A1", start_ts: EPOCH, callsign: "ANA1", flight_id: "12345678901234000000", tier: "settled",
};

// the store reads the URL it is imported under; each case re-seeds the URL and re-reads it (applyUrl's first half)
function setup(inst: string) {
  const api = resetStore(`/?wb=log&wb_inst=${inst}`);
  const fetches = stubFetch();
  const push = vi.spyOn(history, "pushState");
  const replace = vi.spyOn(history, "replaceState");
  const historyCalls = () => [
    ...push.mock.calls.map((c) => ["push", c[2]]),
    ...replace.mock.calls.map((c) => ["replace", c[2]]),
  ];
  return { fetches, historyCalls, calls: api.calls };
}

beforeEach(() => vi.restoreAllMocks());

describe("resolveDeepLink", () => {
  it("a malformed key is dropped without a lookup", async () => {
    const { fetches } = setup("nonsense");
    await resolveDeepLink();
    expect(wb.value.inst).toBeNull();
    expect(fetches).toEqual([]);
  });

  it("an unavailable lookup keeps the URL and says so", async () => {
    const { fetches } = setup(KEY);
    const p = resolveDeepLink();
    expect(fetches[0].url).toMatch(/^\/workbench\/instances\?/);
    fetches[0].notOk();
    await p;
    expect(wb.value.inst).toBe(KEY); // superseded/failed is not not-found
    expect(status.value).toBe("deep link: lookup unavailable");
  });

  it("a resolve whose instance changed under it does nothing", async () => {
    const { fetches, calls } = setup(KEY);
    const p = resolveDeepLink();
    wb.value = { ...wb.value, inst: "40aa11.1785425000.JAL2" }; // a newer navigation owns the state
    fetches[0].ok({ instances: [ROW] });
    await p;
    expect(calls).toEqual([]);
    expect(status.value).toBe("");
  });

  it("a hit focuses the flight and canonicalizes a shifted key", async () => {
    const shifted = `86d3a1.${EPOCH + 5}.ANA1`;
    const { fetches, historyCalls, calls } = setup(shifted);
    const p = resolveDeepLink();
    fetches[0].ok({ instances: [ROW] });
    await p;
    expect(wb.value.inst).toBe(KEY);
    expect(historyCalls()).toEqual([["replace", `/?wb=log&wb_inst=${KEY}&wb_d=30d`]]);
    expect(calls).toEqual(["clearSelection", "dimLive:0.85", "guard:true", `show:${ROW.flight_id}:true`]);
    expect(focusedKey()).toBe(KEY);
    expect(focus.value?.inst.key).toBe(KEY); // the claim IS the signal now — no mirror to fall behind
  });

  it("no candidate falls back to that airframe's day", async () => {
    const { fetches, historyCalls } = setup(KEY);
    const p = resolveDeepLink();
    fetches[0].ok({ instances: [] });
    await p;
    const day = jstDayOf(EPOCH);
    expect(wb.value.inst).toBeNull();
    expect(wb.value.view).toBe("drill");
    expect(wb.value.hex).toBe("86d3a1");
    expect(wb.value.range).toBe(`${day}..${day}`);
    expect(historyCalls()).toEqual([["replace", `/?wb=drill&wb_d=${day}..${day}`]]);
    expect(status.value).toBe("instance not found — showing that day's flights for the airframe");
  });

  it("another flight already in focus is dropped silently before the lookup", async () => {
    const { calls } = setup(KEY);
    let exits = 0;
    enterFocus({ key: "40aa11.1785425000.JAL2", flightId: "777", tier: "settled" } as never,
      { onExit: () => exits++ });
    calls.length = 0;
    resolveDeepLink();
    await flush();
    expect(calls).toEqual(["clearPath", "dimLive:0", "guard:false"]);
    expect(exits).toBe(0);
    expect(focusedKey()).toBeNull();
  });
});

describe("navigate / popstate", () => {
  it("navigate pushes by default, replaces on request, and popstate re-reads the URL imperatively", () => {
    const { historyCalls } = setup("");
    navigate({ page: 2 });
    expect(wb.value.page).toBe(2);
    expect(historyCalls()).toEqual([["push", "/?wb=log&wb_d=30d&wb_p=2"]]);
    navigate({ range: "all" }, { replace: true });
    expect(historyCalls()[1]).toEqual(["replace", "/?wb=log&wb_d=all&wb_p=2"]);
    history.replaceState(null, "", "/?wb=coverage");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(wb.value).toMatchObject({ view: "coverage", page: 1, range: "30d" });
  });

  // an inherited key used to pass the range guard, and RANGE_PRESETS[range] then handed back a function
  it("a wb_d naming an inherited object property still windows on the default range", () => {
    history.replaceState(null, "", "/?wb=log&wb_d=constructor");
    wb.value = readUrl();
    expect(wb.value.range).toBe("30d");
    expect(() => rangeParams.value).not.toThrow();
    const { day_from, day_to } = rangeParams.value;
    expect(day_from).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(day_to).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("an unavailable military filter drops itself once, in one batch", () => {
    setup("");
    navigate({ mil: true, page: 3 });
    const seen: boolean[] = [];
    const unsub = wb.subscribe(() => seen.push(wb.peek().mil));
    milUnavailable();
    unsub();
    expect(wb.value).toMatchObject({ mil: false, page: 1 });
    expect(milAvailable.value).toBe(false);
    expect(status.value).toBe("military filter unavailable — tier mart not deployed");
    expect(seen).toEqual([true, false]); // the subscription's first run, then ONE write
  });
});

// ── the doorway table (plan §1.4) ──────────────────────────────────────────────────────────────────
const DIRTY: WbState = {
  view: "log", range: "all", airline: "ANA", service: "ANA1", od: "HND-ITM", inst: KEY, mil: true,
  flagClass: "diversion", dim: "airline", page: 4, hex: "86d3a1", apt: "HND", type: "B738",
};
const dirty = () => {
  history.replaceState(null, "", "/");
  wb.value = { ...DIRTY };
};

describe("doorway table", () => {
  it("a doorway's reset covers every scope key the target view reads", () => {
    for (const target of Object.keys(VIEW_SCOPE) as (keyof typeof VIEW_SCOPE)[]) {
      dirty();
      doorway(target);
      expect(wb.value.view).toBe(target);
      for (const k of VIEW_SCOPE[target])
        expect({ view: target, k, v: wb.value[k] }).toEqual({ view: target, k, v: SCOPE_DEFAULTS[k] });
      // and nothing the target does not read is left standing either
      for (const k of SCOPE_KEYS) expect(wb.value[k]).toEqual(SCOPE_DEFAULTS[k]);
      expect(wb.value.range).toBe("all"); // the rail's range is chrome, not view scope
      expect(wb.value.inst).toBe(KEY); // and the focus link is not scope: only exitFocus drops it
    }
  });

  it("a carried key the target reads lands; one it does not read is dropped", () => {
    dirty();
    doorway("flags", { flagClass: "military", airline: "JAL", mil: true, page: 7 });
    expect(wb.value).toMatchObject({ view: "flags", flagClass: "military", airline: null, mil: false, page: 1 });
    dirty();
    doorway("trends", { dim: "airport", od: "HND-ITM" });
    expect(wb.value).toMatchObject({ view: "trends", dim: "airport", od: null });
  });

  it("the view tabs carry the whole scope and the target keeps only what it reads", () => {
    dirty();
    doorway("log", wb.peek()); // the ViewTabs click
    expect(wb.value).toMatchObject({
      view: "log", airline: "ANA", service: "ANA1", od: "HND-ITM", hex: "86d3a1", apt: "HND",
      type: "B738", mil: true, page: 1, flagClass: null, dim: "route",
    });
    doorway("flags", wb.peek());
    expect(wb.value).toMatchObject({ view: "flags", flagClass: null, mil: false, airline: null });
    dirty();
    doorway("flags", wb.peek());
    expect(wb.value.flagClass).toBe("diversion"); // the class filter survives into its owning view
  });

  it("every hand-coded reset site is a doorway now", () => {
    const sites: [string, () => void, Partial<WbState>][] = [
      ["search → airline", () => openAirline("JAL"), { view: "drill", airline: "JAL" }],
      ["search → service", () => openService("JAL101", "JAL"), { view: "drill", airline: "JAL", service: "JAL101" }],
      ["search → airframe", () => openAirframe("86d3a1"), { view: "drill", hex: "86d3a1" }],
      ["search → airport", () => openAirport("HND"), { view: "drill", apt: "HND" }],
      ["overview flights cell", () => doorway("log"), { view: "log" }],
      ["overview flagged cell / flags panel", () => doorway("flags"), { view: "flags" }],
      ["overview class chip", () => doorway("flags", { flagClass: "diversion" }), { view: "flags", flagClass: "diversion" }],
      ["overview mil-less log doorway keeps no page", () => doorway("log", { page: 9 }), { view: "log", page: 1 }],
      ["overview mover row", () => doorway("log", { od: "HND-ITM" }), { view: "log", od: "HND-ITM" }],
      ["overview panel head", () => doorway("trends"), { view: "trends" }],
      ["trends rank row", () => doorway("log", { od: "HND-ITM" }), { view: "log", od: "HND-ITM" }],
      ["log clear", () => doorway("log", { airline: "ANA", service: "ANA1" }), { view: "log", airline: "ANA", service: "ANA1" }],
      ["drill crumb: all", () => doorway("drill"), { view: "drill" }],
      ["drill crumb: airline", () => doorway("drill", { airline: "ANA" }), { view: "drill", airline: "ANA" }],
      ["drill airline row", () => openAirline("ANA"), { view: "drill", airline: "ANA" }],
      ["drill service row", () => openService("ANA1", "ANA"), { view: "drill", airline: "ANA", service: "ANA1" }],
      ["deep-link fallback", () => doorway("drill", { hex: "86d3a1", range: "2026-07-29..2026-07-29" }, { replace: true }),
        { view: "drill", hex: "86d3a1", range: "2026-07-29..2026-07-29" }],
    ];
    for (const [label, go, want] of sites) {
      dirty();
      go();
      const got = wb.value;
      expect({ label, ...Object.fromEntries(Object.keys(want).map((k) => [k, got[k as keyof WbState]])) })
        .toEqual({ label, ...want });
      // whatever the site did not carry is at its default — no leftover from the dirty state
      for (const k of VIEW_SCOPE[want.view!])
        if (!(k in want)) expect({ label, k, v: got[k] }).toEqual({ label, k, v: SCOPE_DEFAULTS[k] });
    }
  });

  // the two places the table is deliberately stricter than the vanilla shell was — pinned so the
  // behaviour delta is a spec, not an accident
  it("a doorway drops scope vanilla used to carry: log→drill loses type, a trends doorway starts at route", () => {
    dirty();
    doorway("drill", wb.peek()); // the log→drill view tab, which vanilla wrote as {view, page:1, mil, flagClass}
    expect(wb.value).toMatchObject({
      view: "drill", airline: "ANA", service: "ANA1", od: "HND-ITM", hex: "86d3a1", apt: "HND",
      type: null, // drill has no type filter, so the log's typecode scope cannot survive the door
    });
    dirty();
    doorway("trends"); // the overview trends panel head, which vanilla wrote as {view:"trends", page:1}
    expect(wb.value).toMatchObject({ view: "trends", dim: "route", page: 1 });
  });

  it("no view hand-codes a scope reset any more — the table owns them", () => {
    // vitest runs with the Vite root (livemap/) as cwd; from the repo root the prefix is one level up
    const root = process.cwd().endsWith("livemap") ? process.cwd() : join(process.cwd(), "livemap");
    const dir = join(root, "src/features/workbench/views");
    const offenders: string[] = [];
    for (const f of readdirSync(dir)) {
      const text = readFileSync(join(dir, f), "utf8");
      for (const m of text.matchAll(/navigate\(\{[^}]*\}/g)) {
        const resets = SCOPE_KEYS.filter((k) => new RegExp(`\\b${k}:\\s*(null|false)`).test(m[0]));
        if (resets.length > 1) offenders.push(`${f}: ${m[0]} (${resets.join(", ")})`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

describe("applyUrl", () => {
  it("reads the URL and exits a focus the new URL does not carry", async () => {
    const { calls } = setup(KEY);
    enterFocus({ key: KEY, flightId: "999", tier: "settled" } as never, {});
    calls.length = 0;
    history.replaceState(null, "", "/?wb=trends");
    applyUrl();
    expect(wb.value.view).toBe("trends");
    expect(calls).toEqual(["clearPath", "dimLive:0", "guard:false"]);
    expect(focus.value).toBeNull();
  });
});
