import { setHistPath, clearHistPath } from "../../trails.js?v=6.41";
import { maybeFitHistPath, clearSelection } from "../../interactions.js?v=6.41";

const DIM = 0.85; // the live fleet recedes to context; the drawn path is the subject
const TIER_LABEL = { settled: "● STL", estimated: "◐ EST", provisional: "○ PRV", none: "· NONE", unknown: "· UNK" };

let live = null; // the shared S handle, held from the moment a focus is CLAIMED (pending included)
let focusSeq = 0; // owns the focus lifecycle; every terminal path (resolve/empty/reject) checks it
let barEl = null;
let ptsChip = null;
let exitCb = null;
let clickGuard = null;

const jstDay = (ts) => (ts == null ? "" : new Date((ts + 32400) * 1000).toISOString().slice(0, 10));

function chip(cls, txt) {
  const el = document.createElement("span");
  el.className = cls;
  el.textContent = txt;
  return el;
}

function showBar(inst, n) {
  hideBar();
  barEl = document.createElement("div");
  barEl.className = "wb-focus";
  barEl.setAttribute("role", "region");
  barEl.setAttribute("aria-label", "Focused flight");
  barEl.append(chip("wb-kick", "FOCUS"), chip("wb-cs", inst.callsign || inst.hex || "—"));
  const meta = [inst.day || jstDay(inst.startTs), inst.reg, inst.o || inst.d ? `${inst.o || "?"} → ${inst.d || "?"}` : ""]
    .filter(Boolean)
    .join(" · ");
  if (meta) barEl.append(chip("wb-meta", meta));
  barEl.append(chip(`wb-tier t-${inst.tier}`, TIER_LABEL[inst.tier] || TIER_LABEL.unknown));
  ptsChip = chip("wb-meta", n == null ? "loading path…" : `${n.toLocaleString()} pts`);
  barEl.append(ptsChip);
  const exit = document.createElement("button");
  exit.type = "button";
  exit.className = "wb-exit";
  exit.setAttribute("aria-label", "Exit focus");
  exit.textContent = "✕";
  exit.addEventListener("click", exitFocus);
  barEl.append(exit);
  document.body.appendChild(barEl);
}

function hideBar() {
  if (barEl) barEl.remove();
  barEl = null;
  ptsChip = null;
}

// Live picking is off while dimmed, so a bare map click reaches the spotlight's clear handler and
// would wipe the drawn path out from under the focus bar — swallow it before maplibre dispatches.
function setGuard(on) {
  const el = document.getElementById("map");
  if (!el) return;
  if (on && !clickGuard) {
    clickGuard = (e) => e.stopPropagation();
    el.addEventListener("click", clickGuard, true);
  } else if (!on && clickGuard) {
    el.removeEventListener("click", clickGuard, true);
    clickGuard = null;
  }
}

// silent = a newer focus is superseding this one: its exit callback drops wb_inst from the URL,
// which would erase the very deep link being restored.
function teardown(silent) {
  focusSeq++; // orphan any pending focus fetch — its handlers re-check ownership before acting
  if (!live) return;
  const S = live;
  live = null;
  S.pathFetchSeq++; // orphan an in-flight focus fetch
  clearHistPath();
  S.histPathN = 0;
  S.dimLive = 0;
  S.focus = null;
  setGuard(false);
  hideBar();
  const cb = exitCb;
  exitCb = null;
  if (!silent && cb) cb();
}

export function enterFocus(S, inst, opts = {}) {
  const empty = opts.onEmpty || (() => {});
  if (!inst || !inst.flightId) return empty("no recorded path");
  teardown(true);
  // the spotlight drives this same path pipeline and its rows sit outside #map's click guard —
  // dropping the live selection keeps one pivot, so nothing can overwrite the focused path
  clearSelection();
  const my = ++focusSeq;
  const seq = ++S.pathFetchSeq; // shared with the spotlight's /path flow — the newest request wins
  // Ownership is claimed BEFORE the fetch resolves: dim + guard + bar land now, so Esc/Back can
  // cancel a pending focus and a live-aircraft pick can no longer race the request in flight.
  S.dimLive = DIM;
  S.focus = { key: inst.key, callsign: inst.callsign || inst.hex, tier: inst.tier, n: 0 };
  live = S;
  exitCb = opts.onExit || null;
  setGuard(true);
  showBar(inst, null);
  fetch(`/path/${encodeURIComponent(inst.flightId)}`, { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      if (my !== focusSeq || seq !== S.pathFetchSeq) return; // superseded or exited while pending
      const n = j ? setHistPath(j.points) : 0;
      if (!n) {
        teardown(true);
        return empty("no recorded path");
      }
      // histFlightId is the spotlight's sighting handle — leaving it set would aim its estimate
      // button (and its highlighted row) at a flight whose geometry is no longer on the map
      S.histFlightId = null;
      S.histPathN = n;
      S.focus.n = n;
      if (ptsChip) ptsChip.textContent = `${n.toLocaleString()} pts`;
      maybeFitHistPath(S.histPts);
    })
    .catch(() => {
      if (my !== focusSeq) return; // a stale rejection must never tear down a newer focus
      teardown(true);
      empty("path unavailable");
    });
}

export function exitFocus() {
  teardown(false);
}

// silent teardown for supersession: no exit callback, so the incoming deep link's URL survives
export function dropFocus() {
  teardown(true);
}

export const isFocused = () => live !== null;

window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") exitFocus();
});
