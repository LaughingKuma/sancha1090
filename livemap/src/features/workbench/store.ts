import { batch, computed, signal } from "@preact/signals";
import type { MapFacade } from "../../map/facade";
import { CUSTOM_RANGE_RE, DEFAULTS, RANGE_PRESETS, readUrl, writeUrl, type ViewName, type WbState } from "./url";
import { fetchInstances, type Instance } from "./data";
import { pickCandidate } from "./resolve";

export type { WbState, ViewName };

export interface Focus { inst: Instance; n: number | null }
export interface Flash { key: string | null; msg: string } // key is the ROW key, not the flight's
export interface NavOpts { replace?: boolean }

export const PAGE_LIMIT = 50;
// glyph and pill label side by side: the tier-mix chips and TierPill must draw the same glyphs
export const TIER_GLYPH: Record<string, string> = { settled: "●", estimated: "◐", provisional: "○", none: "·" };
export const TIER_LABEL: Record<string, string> = {
  settled: "● STL", estimated: "◐ EST", provisional: "○ PRV", none: "· NONE", unknown: "· UNK",
};

// URL is the store: wb mirrors it, and every field a view fetches on lives here — never on a component
export const wb = signal<WbState>(readUrl());
// runtime-only, unserialized: focus is the claim on the map, flash a transient row message
export const focus = signal<Focus | null>(null);
export const status = signal("");
export const flash = signal<Flash | null>(null);
export const milAvailable = signal(true); // flipped by an instances envelope when the tier mart is absent

// per-field computeds: a component that reads one re-renders only when that field moves, never on every
// wb write — a focus click must not redraw the tabs or reset the custom-range inputs
export const activeKey = computed(() => wb.value.inst);
export const activeView = computed(() => wb.value.view);
export const activeRange = computed(() => wb.value.range);
export const activeAirline = computed(() => wb.value.airline);
export const activeService = computed(() => wb.value.service);
export const activeHex = computed(() => wb.value.hex);
export const activeApt = computed(() => wb.value.apt);
export const activeType = computed(() => wb.value.type);
export const activeOd = computed(() => wb.value.od);
export const activeMil = computed(() => wb.value.mil);
export const activePage = computed(() => wb.value.page);
// drill's level machine: a search hit on an airframe or airport enters at the instance level
export const drillLevel = computed(() =>
  wb.value.service || wb.value.hex || wb.value.apt ? "instances" : wb.value.airline ? "services" : "airlines");

// The server windows on JST calendar days — shift, then read the UTC date parts.
export const jstDay = (ms: number) => new Date(ms + 9 * 3600 * 1000).toISOString().slice(0, 10);
export const jstDayOf = (ts: number | null | undefined) => (ts == null ? "" : jstDay(ts * 1000));

export const customRange = computed(() => CUSTOM_RANGE_RE.exec(activeRange.value));

export const rangeParams = computed((): { day_from?: string; day_to?: string } => {
  const range = wb.value.range;
  if (range === "all") return {};
  const custom = customRange.value;
  if (custom) return { day_from: custom[1], day_to: custom[2] };
  const days = RANGE_PRESETS[range] || 30;
  return { day_from: jstDay(Date.now() - (days - 1) * 86400000), day_to: jstDay(Date.now()) };
});

const setStatus = (msg: string) => (status.value = msg);

// ── navigation ────────────────────────────────────────────────────────────────────────────────────
// imperative by design: history is written where the state changes, never from an effect watching it
export function navigate(patch: Partial<WbState>, opts: NavOpts = {}) {
  batch(() => {
    const next = { ...wb.value, ...patch };
    wb.value = next;
    writeUrl(next, !!opts.replace);
  });
}

// The doorway table: the scope keys each view actually reads. A doorway navigates with exactly the scope
// its number was computed under and drops every key the target view does not read.
export const SCOPE_KEYS = [
  "airline", "service", "od", "hex", "apt", "type", "mil", "flagClass", "dim", "page",
] as const;
export type ScopeKey = (typeof SCOPE_KEYS)[number];
// doorway resets come from the same DEFAULTS the URL reader falls back to — one spelling of "clean"
export const SCOPE_DEFAULTS = Object.fromEntries(
  SCOPE_KEYS.map((k) => [k, DEFAULTS[k]]),
) as Pick<WbState, ScopeKey>;
export const VIEW_SCOPE: Record<ViewName, readonly ScopeKey[]> = {
  overview: [],
  drill: ["airline", "service", "od", "hex", "apt", "page"],
  log: ["airline", "service", "od", "hex", "apt", "type", "mil", "page"],
  flags: ["flagClass", "page"],
  trends: ["dim", "page"],
  estimates: [],
  coverage: [],
};

export function doorway(target: ViewName, carry: Partial<WbState> = {}, opts: NavOpts = {}) {
  const patch: Partial<WbState> = { view: target, ...SCOPE_DEFAULTS };
  for (const k of SCOPE_KEYS)
    // page is never carried: a doorway lands on the first page of the target, never on the page the
    // number it was clicked from happened to sit at
    if (k !== "page" && carry[k] !== undefined && VIEW_SCOPE[target].includes(k))
      Object.assign(patch, { [k]: carry[k] });
  if (carry.range !== undefined) patch.range = carry.range; // the rail's range is chrome, not view scope
  navigate(patch, opts);
}

// what the active view fetches on: view + range + exactly the scope keys that view reads, so a focus
// click (wb.inst) can never re-fetch a list
export const viewKey = computed(() =>
  JSON.stringify([wb.value.view, wb.value.range, ...VIEW_SCOPE[wb.value.view].map((k) => wb.value[k]), milAvailable.value]),
);

export const openAirline = (name: string) => doorway("drill", { airline: name });
export const openService = (callsign: string, airline: string | null = null) =>
  doorway("drill", { airline, service: callsign });
export const openAirframe = (hex: string) => doorway("drill", { hex });
export const openAirport = (code: string) => doorway("drill", { apt: code });

// the envelope only says so while the mart is absent; drop the filter and re-render once (the reply then
// omits the flag, so this can't loop) — batched, so the rail re-renders once, not twice
export function milUnavailable() {
  batch(() => {
    milAvailable.value = false;
    setStatus("military filter unavailable — tier mart not deployed");
    navigate({ mil: false, page: 1 }, { replace: true });
  });
}

// ── focus (the map claim; ported from the PR3 controller, mirror-free now that it is a signal) ─────
const DIM = 0.85; // the live fleet recedes to context; the drawn path is the subject
const noop = () => {};
let api: MapFacade;
let focusSeq = 0; // owns the focus lifecycle; every terminal path checks it
let exitCb: (() => void) | null = null;

export interface FocusOpts { onExit?: () => void; onEmpty?: (msg: string) => void }

// silent = a newer focus is superseding this one: its exit callback drops wb_inst from the URL,
// which would erase the very deep link being restored.
function teardown(silent: boolean) {
  focusSeq++; // orphan any pending focus fetch — its handlers re-check ownership before acting
  if (!focus.peek()) return;
  api.clearPath();
  api.dimLive(0);
  api.guardMapClicks(false);
  focus.value = null;
  const cb = exitCb;
  exitCb = null;
  if (!silent && cb) cb();
}

export function enterFocus(inst: Instance, opts: FocusOpts = {}) {
  const empty = opts.onEmpty || noop;
  if (!inst || !inst.flightId) return empty("no recorded path");
  teardown(true);
  // the spotlight drives this same path pipeline and its rows sit outside #map's click guard —
  // dropping the live selection keeps one pivot, so nothing can overwrite the focused path
  api.clearSelection();
  const my = ++focusSeq;
  // Ownership is claimed BEFORE the path lands: dim + guard + bar land now, so Esc/Back can cancel a
  // pending focus and a live-aircraft pick can no longer race the request in flight.
  api.dimLive(DIM);
  api.guardMapClicks(true);
  focus.value = { inst, n: null };
  exitCb = opts.onExit || null;
  api.showFlightPath(inst.flightId, { fit: true }).then(({ status: st, n }) => {
    if (my !== focusSeq || st === "superseded") return; // a stale answer never tears down a newer focus
    if (st === "ok") {
      focus.value = { inst, n }; // a fresh record, so the signal notifies the bar
      return;
    }
    teardown(true);
    empty(st === "failed" ? "path unavailable" : "no recorded path");
  }, () => {
    // the facade's contract is status:"failed", never a rejection — but a rejected claim must still
    // release the click guard rather than pin the map behind a stuck "loading path…"
    if (my !== focusSeq) return;
    teardown(true);
    empty("path unavailable");
  });
}

export const exitFocus = () => teardown(false);
// silent teardown for supersession: no exit callback, so the incoming deep link's URL survives
export const dropFocus = () => teardown(true);
export const isFocused = () => focus.peek() !== null;
export const focusedKey = () => focus.peek()?.inst.key ?? null;

// ── the instance claim ────────────────────────────────────────────────────────────────────────────
const dropInst = () => navigate({ inst: null }, { replace: true });

function flashRow(key: string | null, msg: string) {
  setStatus(msg);
  flash.value = { key, msg };
  // transient: the row returns to its route after a beat, never growing a second line
  setTimeout(() => {
    if (flash.peek()?.key === key) flash.value = null;
  }, 2000);
}

export function focusInstance(inst: Instance, rowKey: string | null = inst.key) {
  if (inst.tier === "none" || !inst.flightId) return flashRow(rowKey, "no recorded path");
  navigate({ inst: inst.key }); // a focus step is its own history entry, so Back leaves focus
  enterFocus(inst, {
    onExit: dropInst,
    onEmpty: (msg) => {
      dropInst();
      flashRow(rowKey, msg);
    },
  });
}

// ── URL application (imperative: popstate reads, it never watches wb) ─────────────────────────────
export function applyUrl() {
  wb.value = readUrl();
  if (wb.value.inst) resolveDeepLink();
  else if (isFocused()) exitFocus();
}

// <icao24>.<epoch>[.<callsign>] deep link: nearest start within ±15 min (the key is a start time,
// not an identity — a rebuilt mart may shift it by seconds); the callsign segment breaks real ties.
export async function resolveDeepLink() {
  const want = wb.value.inst;
  const [hex, epochStr, csKey] = String(want).split(".");
  const epoch = Number(epochStr);
  if (!hex || !Number.isFinite(epoch)) {
    wb.value = { ...wb.value, inst: null };
    return;
  }
  const day = jstDayOf(epoch); // the fallback day-list range below still keys on the epoch's own day
  // a different flight already in focus must clear NOW, silently — if this lookup fails, B's path
  // must not sit drawn under A's URL, and A's URL must survive B's teardown
  const focused = focusedKey();
  if (focused && focused !== want) dropFocus();
  // range both JST days: 3,212 live starts sit within 15 min of midnight and a rebuild can cross it
  const p = await fetchInstances(
    { hex, day_from: jstDayOf(epoch - 900), day_to: jstDayOf(epoch + 900), limit: 200 }, "deeplink");
  if (wb.value.inst !== want) return; // a newer navigation owns the state — this resolve is obsolete
  if (!p) return setStatus("deep link: lookup unavailable"); // superseded/failed ≠ not-found: keep the URL
  const hit = pickCandidate(p.rows, epoch, csKey);
  if (hit) {
    // canonicalize to the resolved key: a rebuilt start shifts it, and row lighting compares keys
    if (hit.key && hit.key !== wb.value.inst) navigate({ inst: hit.key }, { replace: true });
    enterFocus(hit, {
      onExit: dropInst,
      // a deep link that draws nothing must not survive the reload it just failed
      onEmpty: (msg) => {
        dropInst();
        setStatus(msg);
      },
    });
    return;
  }
  // no match: fall back to the hex's day list rather than guessing at a neighbouring flight
  wb.value = { ...wb.value, inst: null };
  doorway("drill", { hex, range: `${day}..${day}` }, { replace: true });
  setStatus("instance not found — showing that day's flights for the airframe");
}

let wired = false;
export function initStore(mapApi: MapFacade) {
  api = mapApi;
  if (wired) return; // the harness re-inits per case; the browser mounts once
  wired = true;
  window.addEventListener("popstate", applyUrl);
}
