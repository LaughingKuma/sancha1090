"use strict";

const { MapboxOverlay, IconLayer, ScatterplotLayer, PolygonLayer, PathLayer, TextLayer, PathStyleExtension } = deck;

// 120 s is the MV's data contract (tar1090 position-retention parity) — fade-by-age
// visually recovers freshness within it.
const WINDOW_S = 120;
const RING_NM = [25, 50, 100];
const AMBER = [255, 176, 0];
const MIL = [255, 59, 48];
const KT_TO_MS = 0.514444;
// Beyond this the projection outruns reality (turns, descents) — cap the lead here.
const MAX_DR_S = 15;
// Hold the lead briefly, then settle back onto the last real fix — a frozen row must not
// hold a fabricated position for the rest of the 120 s window. Both must stay > PING_GAP_S
// so any contact that visibly parked fires the acquisition ping on return.
const DR_HOLD_S = 20;
const DR_PARK_S = 26;

// ── Silhouettes ─────────────────────────────────────────────────
// North-pointing top-down silhouettes (64×64, nose up), baked as SVG data-URIs. mask:true means
// deck ignores the fill color and tints by getColor, so age-fade + mil-red apply to any shape.
// All artwork is original, sized from published planform ratios — never traced from tar1090/FA (GPL).
const _svg = (inner, fill = "#fff") =>
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="${fill}">${inner}</svg>`,
  );
const _icon = (inner) => ({ url: _svg(inner), width: 64, height: 64, anchorX: 32, anchorY: 32, mask: true });

const _path = (pts) => `M${pts.replaceAll(" ", " L")} Z`;
// Multi-tone inside one tint: airframe 0.82, cockpit notch cut to 0.3, engine details at 1.0 —
// the mask's alpha carries the tones, so the single getColor tint still drives everything.
const _jet = (body, notch, detail = "") =>
  `<path fill-rule="evenodd" opacity="0.82" d="${_path(body)} ${_path(notch)}"/>` +
  `<polygon opacity="0.3" points="${notch}"/>` +
  detail;

const BODY_NARROW =
  "32,3 35,10 34,24 60,39 60,43 34,31 33,48 39,52 39,55 32,52 25,55 25,52 31,48 30,31 4,43 4,39 30,24 29,10";
const NOTCH_NARROW = "32,5 34.1,9.4 29.9,9.4";
// wider fuselage + broader span than the narrowbody — reads as a heavy twin (777/787/A350)
const BODY_WIDE =
  "32,3 37,12 36,23 61,38 61,43 36,31 35,48 42,53 42,56 32,53 22,56 22,53 29,48 28,31 3,43 3,38 28,23 27,12";
const NOTCH_WIDE = "32,5.5 34.4,10.8 29.6,10.8";
// nacelles poke forward of the leading edge — the tar1090 small-size legibility trick
const NAC_NARROW = '<ellipse cx="25.5" cy="29" rx="2" ry="3.4"/><ellipse cx="38.5" cy="29" rx="2" ry="3.4"/>';
const NAC_WIDE = '<ellipse cx="24.5" cy="28.5" rx="2.5" ry="4"/><ellipse cx="39.5" cy="28.5" rx="2.5" ry="4"/>';
const NAC_QUAD =
  '<ellipse cx="27" cy="29" rx="2.5" ry="3.8"/><ellipse cx="37" cy="29" rx="2.5" ry="3.8"/>' +
  '<ellipse cx="20" cy="32" rx="2.5" ry="3.8"/><ellipse cx="44" cy="32" rx="2.5" ry="3.8"/>';
// near-square planform (span/length ≈ 1.10): blunt wide fuselage, deep-chord wing, 4 big nacelles
const BODY_A380 =
  "32,4 34.3,6.8 36,11 36,21 62,37 62,42 35.5,33 34.5,49 44,55 44,58 32,54.5 20,58 20,55 29.5,49 28.5,33 2,42 2,37 28,21 28,11 29.7,6.8";
const NOTCH_A380 = "32,6.5 34.5,11 29.5,11";
const NAC_A380 =
  '<ellipse cx="41" cy="25.5" rx="2.8" ry="4.4"/><ellipse cx="50" cy="31" rx="2.6" ry="4.1"/>' +
  '<ellipse cx="23" cy="25.5" rx="2.8" ry="4.4"/><ellipse cx="14" cy="31" rx="2.6" ry="4.1"/>';
// longer than wide (span/length ≈ 0.90): hump as a wider forward-fuselage shoulder, sharper sweep
const BODY_B747 =
  "32,2 35,7 35.5,16 34.5,19 34,26 58,43 58,46.5 34,35 33.5,51 41,56.5 41,59.5 32,56 23,59.5 23,56.5 30.5,51 30,35 6,46.5 6,43 30,26 29.5,19 28.5,16 29,7";
const NOTCH_B747 = "32,4 34,7.4 30,7.4";
// the upper deck glows at full alpha — the hump reads even when the shoulder geometry blurs
const HUMP_B747 = '<rect x="30" y="8.5" width="4" height="8.5" rx="2"/>';
const NAC_B747 =
  '<ellipse cx="39.5" cy="30" rx="2.5" ry="4.2"/><ellipse cx="46.5" cy="35.5" rx="2.4" ry="3.9"/>' +
  '<ellipse cx="24.5" cy="30" rx="2.5" ry="4.2"/><ellipse cx="17.5" cy="35.5" rx="2.4" ry="3.9"/>';

const SHAPES = {
  airliner: _jet(BODY_NARROW, NOTCH_NARROW, NAC_NARROW),
  widebody: _jet(BODY_WIDE, NOTCH_WIDE, NAC_WIDE),
  // four nacelles on the generic airframe — fallback for 4-engine types without their own shape
  quad: _jet(BODY_NARROW, NOTCH_NARROW, NAC_QUAD),
  a380: _jet(BODY_A380, NOTCH_A380, NAC_A380),
  b747: _jet(BODY_B747, NOTCH_B747, HUMP_B747 + NAC_B747),
  // straighter wings + two prop discs — turboprop regional
  regional:
    '<polygon points="32,7 34,13 33,24 55,30 55,33 33,28 32,48 37,52 37,54 32,51 27,54 27,52 31,48 31,28 9,33 9,30 31,24 30,13"/>' +
    '<circle cx="16" cy="26" r="4" opacity="0.8"/><circle cx="48" cy="26" r="4" opacity="0.8"/>',
  // light GA: nose prop disc + slim fuselage + STRAIGHT (unswept) high wing — reads as a Cessna, not a jet
  ga:
    '<ellipse cx="32" cy="11" rx="9" ry="1.8" opacity="0.7"/>' +
    '<rect x="30.4" y="10" width="3.2" height="42" rx="1.6"/>' +
    '<rect x="7" y="25" width="50" height="3.6" rx="1.8"/>' +
    '<rect x="22" y="46" width="20" height="3" rx="1.5"/>',
  // rotor disc + crossed blades + tail boom — helicopter
  heli:
    '<circle cx="32" cy="27" r="12" opacity="0.22"/>' +
    '<rect x="30.5" y="27" width="3" height="28" rx="1.5"/><rect x="28" y="51" width="8" height="3" rx="1.5"/>' +
    '<ellipse cx="32" cy="27" rx="5.5" ry="7.5"/>' +
    '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(35 32 27)"/>' +
    '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(-35 32 27)"/>',
};
const SIL = Object.fromEntries(Object.entries(SHAPES).map(([k, v]) => [k, _icon(v)]));

// climb/descend cues — plain triangles, billboarded (never rotated with track)
const CHEV_UP = _icon('<polygon points="32,14 52,50 12,50"/>');
const CHEV_DOWN = _icon('<polygon points="32,50 52,14 12,14"/>');

// body_class → shape + on-screen size (heavies bigger). Unknown class → generic airliner.
const CLASS_SHAPE = {
  quad: "quad", widebody: "widebody", narrowbody: "airliner",
  regional: "regional", ga: "ga", heli: "heli", airliner: "airliner",
};
const SIZE_FOR = {
  quad: 30, widebody: 27, narrowbody: 22, regional: 19, ga: 16, heli: 22, airliner: 21,
};
// Exact-typecode shapes win; body_class stays the fallback; generic airliner last.
const TYPE_ICON = {
  A388: ["a380", 30],
  B748: ["b747", 30], B744: ["b747", 29], B74F: ["b747", 29], BLCF: ["b747", 29],
};
function silShape(a) {
  if (a.is_helicopter === true) return ["heli", SIZE_FOR.heli];
  const t = TYPE_ICON[a.typecode];
  if (t) return t;
  const c = CLASS_SHAPE[a.body_class] ? a.body_class : "airliner";
  return [CLASS_SHAPE[c], SIZE_FOR[c]];
}

// ── Altitude cues ───────────────────────────────────────────────
// Altitude lives inside the amber palette: deep orange on the deck → pale amber at cruise.
const ALT_RAMP = [
  [0, [224, 106, 0]],
  [20000, [255, 176, 0]],
  [40000, [255, 232, 176]],
];
function parseAlt(alt_baro) {
  if (alt_baro === "ground") return 0;
  // the API serializes alt_baro as a string (mixed "ground"/number column upstream)
  const n = typeof alt_baro === "number" ? alt_baro : parseFloat(alt_baro);
  return Number.isFinite(n) ? Math.max(0, n) : null;
}
// Rate from the trail buffer: newest fix vs the oldest fix inside a 20 s window —
// instant deltas off 2 s-spaced fixes are too noisy to threshold.
const VR_WINDOW_S = 20;
const VR_MIN_BASE_S = 8;
const VR_THRESH_FPM = 300;
function verticalState(hex) {
  const tr = trails.get(hex);
  if (!tr || tr.pts.length < 2) return 0;
  const newest = tr.pts[tr.pts.length - 1];
  if (newest.altFt == null) return 0;
  let base = null;
  for (const p of tr.pts) {
    if (newest.ts - p.ts <= VR_WINDOW_S) { base = p; break; }
  }
  if (!base || base === newest || base.altFt == null) return 0;
  const dt = newest.ts - base.ts;
  if (dt < VR_MIN_BASE_S) return 0;
  const fpm = ((newest.altFt - base.altFt) / dt) * 60;
  return fpm > VR_THRESH_FPM ? 1 : fpm < -VR_THRESH_FPM ? -1 : 0;
}
const LABEL_ZOOM = 10.5;
const LABEL_MAX = 40;
function labelText(a) {
  const alt = parseAlt(a.alt_baro);
  const lvl = alt == null ? "" : alt >= 18000 ? ` FL${Math.round(alt / 100)}` : ` ${Math.round(alt)}ft`;
  return `${(a.flight || "").trim()}${lvl}`;
}
function altTint(altFt) {
  if (altFt == null) return AMBER; // no baro alt → classic amber
  const x = Math.min(altFt, 40000);
  const i = x < 20000 ? 0 : 1;
  const [f0, c0] = ALT_RAMP[i];
  const [f1, c1] = ALT_RAMP[i + 1];
  const f = (x - f0) / (f1 - f0);
  return [0, 1, 2].map((k) => Math.round(c0[k] + f * (c1[k] - c0[k])));
}

// Pseudo-3D: the shadow walks toward screen-SE and shrinks as the aircraft climbs (sun fixed NW).
const SHADOW_DIR = [0.45, 0.89];
const SHADOW_MAX_PX = 26;
const shadowPx = (altFt) => Math.min(SHADOW_MAX_PX, (altFt ?? 0) / 1700);

// Great-circle range/bearing from the receiver — feederCenter is [lon, lat] from /range-outline.
function stationVector(lon, lat) {
  if (!feederCenter || lon == null || lat == null) return null;
  const toRad = Math.PI / 180;
  const [flon, flat] = feederCenter;
  const dLat = (lat - flat) * toRad;
  const dLon = (lon - flon) * toRad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(flat * toRad) * Math.cos(lat * toRad) * Math.sin(dLon / 2) ** 2;
  const nm = 2 * 3440.065 * Math.asin(Math.sqrt(a)); // earth radius in nm
  const y = Math.sin(dLon) * Math.cos(lat * toRad);
  const x =
    Math.cos(flat * toRad) * Math.sin(lat * toRad) -
    Math.sin(flat * toRad) * Math.cos(lat * toRad) * Math.cos(dLon);
  const brg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  return { nm, brg };
}

const map = new maplibregl.Map({
  container: "map",
  style: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
  center: [139.69, 35.69], // Tokyo
  zoom: 8,
  attributionControl: { compact: true },
  pitchWithRotate: false,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

const overlay = new MapboxOverlay({
  interleaved: true,
  getTooltip,
  layers: [],
});
map.addControl(overlay);

// ── Snapshot state ──────────────────────────────────────────────
// Anchor the snapshot to the server clock so dead-reckoning and age never jump on a new poll.
let snap = { server_ts: 0, aircraft: [], perf0: 0 };

// a dead feed must read as "display stopped", not as a fleet-wide signal-loss event
const STREAM_FREEZE_S = 3;
function serverNow() {
  return snap.server_ts + Math.min(performance.now() / 1000 - snap.perf0, STREAM_FREEZE_S);
}

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
const renderState = new Map(); // hex → { offset, snapTs, prev, t }
const metresBetween = (dLon, dLat, latRef) =>
  Math.hypot(dLat * 111320, dLon * 111320 * Math.cos((latRef * Math.PI) / 180));

function smoothPos(hex, target, pf) {
  if (!hex) return target; // hex-less rows must not share one easing bucket
  let st = renderState.get(hex);
  if (!st) {
    renderState.set(hex, (st = { offset: [0, 0], snapTs: snap.server_ts, prev: [target[0], target[1]], t: pf }));
    return target;
  }
  if (st.snapTs !== snap.server_ts) {
    const dLon = st.prev[0] - target[0];
    const dLat = st.prev[1] - target[1];
    st.offset = metresBetween(dLon, dLat, target[1]) < EASE_MAX_M ? [dLon, dLat] : [0, 0];
    st.snapTs = snap.server_ts;
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
const finiteTs = (...vals) => {
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

function frameData() {
  const t = serverNow();
  const pf = performance.now() / 1000;
  return snap.aircraft.map((a) => {
    const age = fixAge(a, t);
    const fade = Math.min(1, age / WINDOW_S);
    const alpha = Math.max(0.12, 1 - 0.85 * fade); // fresh = bright, fringe = dim
    const mil = a.is_military === true;
    const [shape, size] = silShape(a);
    const altFt = parseAlt(a.alt_baro);
    const base = mil ? MIL : altTint(altFt);
    const tint =
      age >= DR_PARK_S ? base.map((c, k) => Math.round(c + STALE_BLEND * (STALE_GREY[k] - c))) : base;
    return { a, pos: smoothPos(a.hex, deadReckon(a, age), pf), age, alpha, mil, shape, size, altFt, vs: verticalState(a.hex), tint };
  });
}

// ── Trails ──────────────────────────────────────────────────────
// Last ~90 s of real fixes per hex, accumulated client-side from the poll — zero backend.
const TRAIL_S = 90;
const TRAIL_GAP_S = 2; // sub-2s fixes add segments without adding visible shape
const GAP_EST_S = MAX_DR_S; // a gap the DR envelope couldn't cover on screen is estimated, not flown track
const trails = new Map();
let trailSegments = [];
function ingestTrails() {
  for (const a of snap.aircraft) {
    if (!a.hex || a.lon == null || a.lat == null || a.capture_ts == null) continue;
    // coerce before clock math — a malformed ts would NaN the pruning and strand the trail
    const captureTs = Number(a.capture_ts);
    if (!Number.isFinite(captureTs)) continue;
    let tr = trails.get(a.hex);
    if (!tr) trails.set(a.hex, (tr = { pts: [], mil: false }));
    tr.mil = a.is_military === true;
    const last = tr.pts[tr.pts.length - 1];
    if (!last || (captureTs - last.ts >= TRAIL_GAP_S && (a.lon !== last.lon || a.lat !== last.lat)))
      tr.pts.push({ lon: a.lon, lat: a.lat, ts: captureTs, altFt: parseAlt(a.alt_baro), est: !!last && captureTs - last.ts > GAP_EST_S });
  }
}
// Rebuilt on its own clock so trails keep fading through stream errors; 1 Hz is invisible
// at a 90 s fade and far cheaper than rebuilding per frame.
function rebuildTrailSegments() {
  const t = serverNow();
  const segs = [];
  for (const [, tr] of trails) {
    // keep the newest fix alive as the bridge anchor — trails die with their aircraft, not by clock
    while (tr.pts.length > 1 && t - tr.pts[0].ts > TRAIL_S) tr.pts.shift();
    for (let i = 1; i < tr.pts.length; i++) {
      const p = tr.pts[i];
      const fresh = Math.max(0, 1 - (t - p.ts) / TRAIL_S);
      segs.push({
        path: [[tr.pts[i - 1].lon, tr.pts[i - 1].lat], [p.lon, p.lat]],
        color: [...(tr.mil ? MIL : altTint(p.altFt)), Math.round(145 * fresh * (p.est ? 0.35 : 1))],
        dash: p.est ? [6, 4] : [0, 0],
      });
    }
  }
  trailSegments = segs;
}
setInterval(rebuildTrailSegments, 1000);

// ── Acquisition pings ───────────────────────────────────────────
// Ring only when a hex is NEW or silent >10 s — per-fix pings (1 Hz) would be constant static.
const PING_GAP_S = 10;
const PING_LIFE_S = 1.2;
const lastSeen = new Map();
let pings = [];
function detectAcquisitions() {
  const t = serverNow();
  for (const a of snap.aircraft) {
    if (!a.hex || a.lon == null || a.lat == null) continue;
    const ct = Number(a.capture_ts);
    if (!Number.isFinite(ct)) continue;
    const prev = lastSeen.get(a.hex);
    if (prev === undefined || ct - prev > PING_GAP_S)
      pings.push({ pos: [a.lon, a.lat], t0: t, mil: a.is_military === true });
    lastSeen.set(a.hex, ct);
  }
  for (const [hex, ts] of lastSeen) if (t - ts > 600) lastSeen.delete(hex); // bound memory
}

// Receiver coverage outline + dot — slow-changing, fetched separately from the 1 Hz aircraft poll.
let outlineData = [];
let feederCenter = null;
async function loadOutline() {
  try {
    const j = await (await fetch("/range-outline", { cache: "no-store" })).json();
    feederCenter = j.center || null;
    outlineData = j.ring && j.ring.length ? [{ ring: j.ring }] : [];
  } catch (e) {
    /* outline is optional — absent until the batch job has run */
  }
}
loadOutline();
setInterval(loadOutline, 300000);

function buildLayers() {
  const tNow = serverNow();
  pings = pings.filter((p) => tNow - p.t0 < PING_LIFE_S);
  // crowded frame → demand one more zoom level before labels appear
  const labelZoom = LABEL_ZOOM + (snap.aircraft.length > LABEL_MAX ? 1 : 0);
  const showLabels = map.getZoom() >= labelZoom;
  const data = frameData();
  // elastic band: the wake terminates at the rendered icon in every state (tar1090's rule);
  // dimmer than recorded track, and suppressed mid-ease so it never sweeps the coverage hole
  const bridges = [];
  for (const d of data) {
    const tr = trails.get(d.a.hex);
    const head = tr && tr.pts[tr.pts.length - 1];
    if (!head) continue;
    const st = renderState.get(d.a.hex);
    if (st && metresBetween(st.offset[0], st.offset[1], d.pos[1]) > 400) continue;
    if (head.lon !== d.pos[0] || head.lat !== d.pos[1])
      bridges.push({ path: [[head.lon, head.lat], d.pos], color: [...d.tint, Math.round(87 * d.alpha)] });
  }
  return [
    // station range rings — beneath everything; fresh data array each frame so a late
    // feederCenter fetch is picked up (deck only recomputes attributes on data change)
    new ScatterplotLayer({
      id: "range-rings",
      data: feederCenter ? RING_NM.map((nm) => ({ nm })) : [],
      getPosition: () => feederCenter,
      getRadius: (d) => d.nm * 1852,
      radiusUnits: "meters",
      stroked: true,
      filled: false,
      getLineColor: [78, 162, 174, 90],
      getLineWidth: 1,
      lineWidthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    new TextLayer({
      id: "range-ring-labels",
      data: feederCenter ? RING_NM.map((nm) => ({ nm })) : [],
      getPosition: (d) => [feederCenter[0], feederCenter[1] + d.nm / 60], // 1 nm = 1/60° lat
      getText: (d) => `${d.nm} nm`,
      getSize: 10,
      getColor: [78, 162, 174, 150],
      fontFamily: "'Spline Sans Mono', monospace",
      getTextAnchor: "middle",
      getAlignmentBaseline: "bottom",
      parameters: { depthTest: false },
    }),
    // coverage polygon, beneath everything — terrain-shaped reception envelope
    new PolygonLayer({
      id: "range-outline",
      data: outlineData,
      getPolygon: (d) => d.ring,
      stroked: true,
      filled: true,
      getFillColor: [24, 116, 130, 20],
      getLineColor: [78, 162, 174, 130],
      getLineWidth: 1.3,
      lineWidthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    // fading wake of real fixes — approach streams into HND/NRT read as structure, not dots
    new PathLayer({
      id: "trails",
      data: trailSegments,
      getPath: (d) => d.path,
      getColor: (d) => d.color,
      getWidth: 1.8,
      widthUnits: "pixels",
      capRounded: true,
      getDashArray: (d) => d.dash,
      extensions: [new PathStyleExtension({ dash: true })],
      parameters: { depthTest: false },
    }),
    new PathLayer({
      id: "trail-bridge",
      data: bridges,
      getPath: (d) => d.path,
      getColor: (d) => d.color,
      getWidth: 1.8,
      widthUnits: "pixels",
      capRounded: true,
      parameters: { depthTest: false },
    }),
    // altitude ground-shadow: same mask in black, offset/shrunk with height — cruisers fly above the map
    new IconLayer({
      id: "shadows",
      data,
      getIcon: (d) => SIL[d.shape],
      getPosition: (d) => d.pos,
      getAngle: (d) => -(d.a.track ?? 0),
      getColor: (d) => [0, 0, 0, Math.round(d.alpha * 95)],
      getSize: (d) => d.size * (1 - (0.18 * Math.min(d.altFt ?? 0, 40000)) / 40000),
      getPixelOffset: (d) => {
        const px = shadowPx(d.altFt);
        return [SHADOW_DIR[0] * px, SHADOW_DIR[1] * px];
      },
      sizeUnits: "pixels",
      billboard: true,
      parameters: { depthTest: false },
    }),
    new ScatterplotLayer({
      id: "pings",
      data: pings,
      getPosition: (p) => p.pos,
      getRadius: (p) => 6 + 34 * ((tNow - p.t0) / PING_LIFE_S),
      radiusUnits: "pixels",
      stroked: true,
      filled: false,
      getLineColor: (p) => [...(p.mil ? MIL : AMBER), Math.round(160 * (1 - (tNow - p.t0) / PING_LIFE_S))],
      getLineWidth: 1.5,
      lineWidthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    // Soft phosphor glow under each contact — military burns hotter and wider.
    new ScatterplotLayer({
      id: "glow",
      data,
      getPosition: (d) => d.pos,
      getRadius: (d) => (d.mil ? 15 : Math.max(8, d.size * 0.42)), // bigger airframe, bigger glow
      radiusUnits: "pixels",
      getFillColor: (d) => [...d.tint, Math.round(d.alpha * (d.mil ? 130 : 70))],
      stroked: false,
      parameters: { depthTest: false },
    }),
    // near-black rim under the tinted icon — crisp edge against bright map areas
    new IconLayer({
      id: "halo",
      data,
      getIcon: (d) => SIL[d.shape],
      getPosition: (d) => d.pos,
      getAngle: (d) => -(d.a.track ?? 0),
      getColor: (d) => [10, 12, 15, Math.round(d.alpha * 235)],
      getSize: (d) => d.size * 1.22,
      sizeUnits: "pixels",
      billboard: true,
      parameters: { depthTest: false },
    }),
    new IconLayer({
      id: "planes",
      data,
      getIcon: (d) => SIL[d.shape], // silhouette per typecode/class
      getPosition: (d) => d.pos,
      getAngle: (d) => -(d.a.track ?? 0), // deck angle is CCW; heading is CW from north
      getColor: (d) => [...d.tint, Math.round(d.alpha * 255)],
      getSize: (d) => d.size,
      sizeUnits: "pixels",
      billboard: true,
      pickable: true,
      parameters: { depthTest: false },
    }),
    // ▲/▼ beside the icon; data is re-filtered every frame so tint/alpha stay live
    new IconLayer({
      id: "chevrons",
      data: data.filter((d) => d.vs !== 0),
      getIcon: (d) => (d.vs > 0 ? CHEV_UP : CHEV_DOWN),
      getPosition: (d) => d.pos,
      getColor: (d) => [...d.tint, Math.round(d.alpha * 230)],
      getSize: 7,
      sizeUnits: "pixels",
      getPixelOffset: (d) => [d.size * 0.7 + 5, 0],
      billboard: true,
      parameters: { depthTest: false },
    }),
    new TextLayer({
      id: "labels",
      data: showLabels ? data.filter((d) => d.a.flight) : [],
      getPosition: (d) => d.pos,
      getText: (d) => labelText(d.a),
      getSize: 10,
      getColor: (d) => [...d.tint, Math.round(d.alpha * 200)],
      getPixelOffset: (d) => [0, d.size * 0.7 + 10],
      fontFamily: "'Spline Sans Mono', monospace",
      getTextAnchor: "middle",
      getAlignmentBaseline: "top",
      billboard: true,
      parameters: { depthTest: false },
    }),
    // the receiver itself — small bright dot with a dark ring (tar1090-style)
    new ScatterplotLayer({
      id: "receiver",
      data: feederCenter ? [feederCenter] : [],
      getPosition: (d) => d,
      getRadius: 4.5,
      radiusUnits: "pixels",
      getFillColor: [232, 238, 245, 235],
      stroked: true,
      getLineColor: [5, 9, 14, 255],
      lineWidthUnits: "pixels",
      getLineWidth: 1.6,
      parameters: { depthTest: false },
    }),
  ];
}

// 60fps glide loop, decoupled from the 1 Hz server poll.
function tick() {
  overlay.setProps({ layers: buildLayers() });
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// ── Tooltip ─────────────────────────────────────────────────────
// ADS-B callsigns/hex are attacker-transmittable and deck.gl renders `html` as innerHTML
const esc = (v) =>
  String(v ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

function getTooltip(info) {
  if (!info || !info.object) return null;
  const a = info.object.a;
  const mil = a.is_military === true;
  const badges =
    (mil ? '<span class="badge">MIL</span>' : "") +
    (a.is_helicopter ? '<span class="badge">HELI</span>' : "");
  const alt = a.alt_baro == null ? "—" : a.alt_baro === "ground" ? "GROUND" : `${a.alt_baro} ft`;
  const spd = a.gs == null ? "—" : `${Math.round(a.gs)} kt`;
  const hdg = a.track == null ? "—" : `${Math.round(a.track)}°`;
  const fixTs = finiteTs(a.pos_ts, a.capture_ts);
  const fage = fixTs == null ? NaN : serverNow() - fixTs;
  const contact = !Number.isFinite(fage) ? "—" : fage < 5 ? "live" : `last fix ${Math.round(fage)} s ago`;
  const sv = stationVector(a.lon, a.lat);
  // Backstory ring (v5.1): latest known route for this callsign from the flights catalog.
  // D-2-sourced rows carry an old departure time — show the clock only when it's today's leg.
  let routeLine = "";
  if (a.route) {
    const dep = a.route.departed_epoch;
    const ageH = dep ? (Date.now() / 1000 - dep) / 3600 : Infinity;
    const when =
      ageH < 24
        ? ` · departed ${new Date(dep * 1000).toTimeString().slice(0, 5)}`
        : " · usual route";
    routeLine = `<div class="route">${esc(a.route.origin)} → ${esc(a.route.dest)}${esc(when)}</div>`;
  }
  const html =
    `<div class="flight ${mil ? "mil" : ""}">${esc(a.flight || a.hex || "UNKNOWN")}${badges}</div>` +
    `<div class="org">${esc(a.airline_name || "Unregistered callsign")}</div>` +
    routeLine +
    "<dl>" +
    `<dt>Type</dt><dd>${esc(a.aircraft_desc || a.typecode || "—")}</dd>` +
    `<dt>Code</dt><dd>${esc(a.typecode || "—")}</dd>` +
    `<dt>Reg</dt><dd>${esc(a.registration || "—")}</dd>` +
    `<dt>ICAO</dt><dd>${esc((a.hex || "—").toUpperCase())}</dd>` +
    `<dt>Origin</dt><dd>${esc(a.reg_country || "—")}</dd>` +
    `<dt>Alt</dt><dd>${esc(alt)}</dd>` +
    `<dt>Speed</dt><dd>${esc(spd)}</dd>` +
    `<dt>Heading</dt><dd>${esc(hdg)}</dd>` +
    `<dt>Range</dt><dd>${esc(sv ? sv.nm.toFixed(1) + " nm" : "—")}</dd>` +
    `<dt>Bearing</dt><dd>${esc(sv ? Math.round(sv.brg) + "°" : "—")}</dd>` +
    `<dt>Recv</dt><dd>${esc(a.recv || "—")}</dd>` +
    `<dt>Contact</dt><dd>${esc(contact)}</dd>` +
    "</dl>";
  return { html, className: "ac-tip" };
}

// ── Poll the server-side cache (one shared query stream, never one per tab) ──
const fmt = (n) => String(n).padStart(2, "0");
let pollInFlight = false;
async function poll() {
  if (pollInFlight) return; // never let a slow response race a newer one
  pollInFlight = true;
  try {
    const r = await fetch("/aircraft", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    // coerced: a string ts would turn serverNow() into concatenation and NaN all clock math
    const serverTs = Number(j.server_ts);
    // duplicates too: re-anchoring perf0 on an equal ts steps serverNow() backward (DR stutter)
    if (!Number.isFinite(serverTs) || serverTs <= snap.server_ts) {
      // server reachable but feed not advancing — distinct from the fetch-error path below
      if (performance.now() / 1000 - snap.perf0 > STREAM_FREEZE_S)
        document.getElementById("meta-line").textContent = "Stream stalled — waiting…";
      return;
    }
    snap = { server_ts: serverTs, aircraft: j.aircraft || [], perf0: performance.now() / 1000 };
    // absence from the accepted snapshot is the one authority on "gone" (MV 120 s expiry)
    const live = new Set(snap.aircraft.map((a) => a.hex));
    for (const hex of trails.keys()) if (!live.has(hex)) trails.delete(hex);
    for (const hex of renderState.keys()) if (!live.has(hex)) renderState.delete(hex);
    ingestTrails();
    rebuildTrailSegments();
    detectAcquisitions();

    const total = snap.aircraft.length;
    const milCount = snap.aircraft.filter((a) => a.is_military === true).length;
    document.getElementById("stat-total").textContent = total;
    document.getElementById("stat-mil").textContent = milCount;
    const d = new Date(serverTs * 1000);
    document.getElementById("meta-line").textContent =
      `Synced ${fmt(d.getHours())}:${fmt(d.getMinutes())}:${fmt(d.getSeconds())} · ${total} contacts · shared cache`;
  } catch (e) {
    document.getElementById("meta-line").textContent = `Stream error — retrying… (${e.message})`;
  } finally {
    pollInFlight = false;
  }
}
poll();
setInterval(poll, 500);

// ── ?icons — debug strip: every shape at authoring + on-map size, plus fade/mil tints ──
if (new URLSearchParams(location.search).has("icons")) {
  const onMap = { a380: 30, b747: 30, quad: 30, widebody: 27, airliner: 22, regional: 19, ga: 16, heli: 22 };
  const strip = document.createElement("div");
  strip.style.cssText =
    "position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:40;display:flex;gap:18px;" +
    "padding:14px 18px 10px;background:rgba(5,9,14,0.92);border:1px solid rgba(255,176,0,0.25);" +
    "font:10px 'Spline Sans Mono',monospace;color:#7e93a8;text-align:center;";
  const img = (name, px, fill, op = 1) =>
    `<img src="${_svg(SHAPES[name], fill)}" width="${px}" height="${px}" style="opacity:${op}">`;
  for (const [name, size] of Object.entries(onMap)) {
    strip.insertAdjacentHTML(
      "beforeend",
      `<div style="display:flex;flex-direction:column;align-items:center;gap:6px;">` +
        `<div style="display:flex;align-items:flex-end;">${img(name, 64, "#ffb000")}</div>` +
        `<div style="display:flex;align-items:center;gap:6px;">` +
        `${img(name, size, "#ffb000")}${img(name, size, "#ffb000", 0.35)}${img(name, size, "#ff3b30")}</div>` +
        `<span>${name} · ${size}px</span></div>`,
    );
  }
  document.body.appendChild(strip);
}
