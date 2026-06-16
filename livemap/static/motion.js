import { S, serverNow } from "./state.js";
import { KT_TO_MS, MAX_DR_S, DR_HOLD_S, DR_PARK_S, WINDOW_S, MIL } from "./constants.js";
import { silShape, sizeFor } from "./silhouettes.js";
import { parseAlt, altTint } from "./altitude.js";
import { verticalRate, vsState } from "./telemetry.js";

// glide 0→15 s of lead, hold to 20 s, settle back onto the fix by 26 s — continuous, no jumps
function drSeconds(age) {
  if (age <= DR_HOLD_S) return Math.min(age, MAX_DR_S);
  if (age >= DR_PARK_S) return 0;
  return MAX_DR_S * (1 - (age - DR_HOLD_S) / (DR_PARK_S - DR_HOLD_S));
}

function deadReckon(a, age) {
  if (a.gs == null || a.track == null || age <= 0) return [a.lon, a.lat];
  const dist = a.gs * KT_TO_MS * drSeconds(age); // metres flown since the fix
  const br = (a.track * Math.PI) / 180;
  const dLat = (dist * Math.cos(br)) / 111320;
  const dLon = (dist * Math.sin(br)) / (111320 * Math.cos((a.lat * Math.PI) / 180));
  return [a.lon + dLon, a.lat + dLat];
}

// Per-poll target discontinuities (turn corrections, reacquisition snaps — including
// backward ones) decay over ~τ instead of snapping; beyond EASE_MAX_M it's a genuine
// relocation — jump instantly, the acquisition ping already marks it.
const EASE_TAU_S = 0.5;
const EASE_MAX_M = 5000;
export const metresBetween = (dLon, dLat, latRef) =>
  Math.hypot(dLat * 111320, dLon * 111320 * Math.cos((latRef * Math.PI) / 180));

function smoothPos(hex, target, pf) {
  if (!hex) return target; // hex-less rows must not share one easing bucket
  let st = S.renderState.get(hex);
  if (!st) {
    S.renderState.set(hex, (st = { offset: [0, 0], snapTs: S.snap.server_ts, prev: [target[0], target[1]], t: pf }));
    return target;
  }
  if (st.snapTs !== S.snap.server_ts) {
    const dLon = st.prev[0] - target[0];
    const dLat = st.prev[1] - target[1];
    st.offset = metresBetween(dLon, dLat, target[1]) < EASE_MAX_M ? [dLon, dLat] : [0, 0];
    st.snapTs = S.snap.server_ts;
  }
  const decay = Math.exp(-Math.max(0, pf - st.t) / EASE_TAU_S);
  st.offset[0] *= decay;
  st.offset[1] *= decay;
  st.t = pf;
  st.prev = [target[0] + st.offset[0], target[1] + st.offset[1]];
  // copy — deck accessors must never alias the easing state
  return [st.prev[0], st.prev[1]];
}

// a garbage timestamp must fall through to the next candidate, not NaN-poison DR/alpha/tint
export const finiteTs = (...vals) => {
  for (const v of vals) {
    if (v == null || v === "") continue;
    const n = Number(v);
    if (Number.isFinite(n)) return n;
  }
  return null;
};
// seam for a future pos_ts passthrough (seen_pos via the MV); today rows freeze whole,
// so capture_ts IS the fix time
const fixAge = (a, t) => {
  const ts = finiteTs(a.pos_ts, a.capture_ts);
  return Math.max(0, t - (ts ?? t));
};
// parked contacts drift toward grey — signal loss must read as state, not a hover glitch
const STALE_GREY = [148, 163, 178];
const STALE_BLEND = 0.45;

// zm (zoom multiplier) is passed in by buildLayers — keeps this module independent of the map
export function frameData(zm) {
  const t = serverNow();
  const pf = performance.now() / 1000;
  return S.snap.aircraft.map((a) => {
    const age = fixAge(a, t);
    const fade = Math.min(1, age / WINDOW_S);
    const alpha = Math.max(0.12, 1 - 0.85 * fade); // fresh = bright, fringe = dim
    const mil = a.is_military === true;
    const shape = silShape(a);
    let size = sizeFor(a) * zm;
    if (S.selected && a.hex === S.selected.hex) size = Math.max(size, Math.min(size * 1.35, 56)); // spotlight cap
    const altFt = parseAlt(a.alt_baro);
    const base = mil ? MIL : altTint(altFt);
    const tint =
      age >= DR_PARK_S ? base.map((c, k) => Math.round(c + STALE_BLEND * (STALE_GREY[k] - c))) : base;
    return { a, pos: smoothPos(a.hex, deadReckon(a, age), pf), age, alpha, mil, shape, size, altFt, vs: vsState(verticalRate(a)), tint };
  });
}
