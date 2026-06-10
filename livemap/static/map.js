"use strict";

const { MapboxOverlay, IconLayer, ScatterplotLayer, PolygonLayer } = deck;

// 120 s is the MV's data contract (tar1090 position-retention parity) — fade-by-age
// visually recovers freshness within it.
const WINDOW_S = 120;
const AMBER = [255, 176, 0];
const MIL = [255, 59, 48];
const KT_TO_MS = 0.514444;
// Beyond this the projection outruns reality (turns, descents) — hold the capped estimate.
const MAX_DR_S = 15;

// North-pointing top-down silhouettes (64×64, nose up), baked as SVG data-URIs. mask:true means
// deck ignores the white fill and tints by getColor, so the age-fade + mil-red still apply.
const _svg = (inner) =>
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64" fill="#fff">${inner}</svg>`,
  );
const _icon = (inner) => ({ url: _svg(inner), width: 64, height: 64, anchorX: 32, anchorY: 32, mask: true });

// Swept-wing twin (used for narrowbody/widebody/generic — size carries the wide/narrow difference).
const _AIRLINER =
  '<polygon points="32,3 35,10 34,24 60,39 60,43 34,31 33,48 39,52 39,55 32,52 25,55 25,52 31,48 30,31 4,43 4,39 30,24 29,10"/>';
// wider fuselage + broader span than the narrowbody — reads as a heavy twin (777/787/A350)
const _WIDEBODY =
  '<polygon points="32,3 37,12 36,23 61,38 61,43 36,31 35,48 42,53 42,56 32,53 22,56 22,53 29,48 28,31 3,43 3,38 28,23 27,12"/>';
const SIL = {
  airliner: _icon(_AIRLINER),
  widebody: _icon(_WIDEBODY),
  // four engine nacelles on the wings — reads as a jumbo
  quad: _icon(
    _AIRLINER +
      '<ellipse cx="20" cy="33" rx="2.3" ry="3.6"/><ellipse cx="27" cy="30" rx="2.3" ry="3.6"/>' +
      '<ellipse cx="44" cy="33" rx="2.3" ry="3.6"/><ellipse cx="37" cy="30" rx="2.3" ry="3.6"/>',
  ),
  // straighter wings + two prop discs — turboprop regional
  regional: _icon(
    '<polygon points="32,7 34,13 33,24 55,30 55,33 33,28 32,48 37,52 37,54 32,51 27,54 27,52 31,48 31,28 9,33 9,30 31,24 30,13"/>' +
      '<circle cx="16" cy="26" r="4" opacity="0.8"/><circle cx="48" cy="26" r="4" opacity="0.8"/>',
  ),
  // light GA: nose prop disc + slim fuselage + STRAIGHT (unswept) high wing — reads as a Cessna, not a jet
  ga: _icon(
    '<ellipse cx="32" cy="11" rx="9" ry="1.8" opacity="0.7"/>' +
      '<rect x="30.4" y="10" width="3.2" height="42" rx="1.6"/>' +
      '<rect x="7" y="25" width="50" height="3.6" rx="1.8"/>' +
      '<rect x="22" y="46" width="20" height="3" rx="1.5"/>',
  ),
  // rotor disc + crossed blades + tail boom — helicopter
  heli: _icon(
    '<circle cx="32" cy="27" r="12" opacity="0.22"/>' +
      '<rect x="30.5" y="27" width="3" height="28" rx="1.5"/><rect x="28" y="51" width="8" height="3" rx="1.5"/>' +
      '<ellipse cx="32" cy="27" rx="5.5" ry="7.5"/>' +
      '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(35 32 27)"/>' +
      '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(-35 32 27)"/>',
  ),
};
// body_class → which silhouette + on-screen size (heavies bigger). Unknown class → generic airliner.
const ICON_FOR = {
  quad: SIL.quad, widebody: SIL.widebody, narrowbody: SIL.airliner,
  regional: SIL.regional, ga: SIL.ga, heli: SIL.heli, airliner: SIL.airliner,
};
const SIZE_FOR = {
  quad: 30, widebody: 27, narrowbody: 22, regional: 19, ga: 16, heli: 22, airliner: 21,
};
function silClass(a) {
  if (a.is_helicopter === true) return "heli";
  return ICON_FOR[a.body_class] ? a.body_class : "airliner";
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

function serverNow() {
  return snap.server_ts + (performance.now() / 1000 - snap.perf0);
}

function deadReckon(a, age) {
  if (a.gs == null || a.track == null || age <= 0) return [a.lon, a.lat];
  const dist = a.gs * KT_TO_MS * Math.min(age, MAX_DR_S); // metres flown since the fix
  const br = (a.track * Math.PI) / 180;
  const dLat = (dist * Math.cos(br)) / 111320;
  const dLon = (dist * Math.sin(br)) / (111320 * Math.cos((a.lat * Math.PI) / 180));
  return [a.lon + dLon, a.lat + dLat];
}

function frameData() {
  const t = serverNow();
  return snap.aircraft.map((a) => {
    const age = Math.max(0, t - (a.capture_ts ?? t));
    const fade = Math.min(1, age / WINDOW_S);
    const alpha = Math.max(0.12, 1 - 0.85 * fade); // fresh = bright, fringe = dim
    return { a, pos: deadReckon(a, age), alpha, mil: a.is_military === true, cls: silClass(a) };
  });
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
  const data = frameData();
  return [
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
    // Soft phosphor glow under each contact — military burns hotter and wider.
    new ScatterplotLayer({
      id: "glow",
      data,
      getPosition: (d) => d.pos,
      getRadius: (d) => (d.mil ? 15 : Math.max(8, SIZE_FOR[d.cls] * 0.42)), // bigger airframe, bigger glow
      radiusUnits: "pixels",
      getFillColor: (d) => [...(d.mil ? MIL : AMBER), Math.round(d.alpha * (d.mil ? 130 : 70))],
      stroked: false,
      parameters: { depthTest: false },
    }),
    new IconLayer({
      id: "planes",
      data,
      getIcon: (d) => ICON_FOR[d.cls], // silhouette per aircraft class
      getPosition: (d) => d.pos,
      getAngle: (d) => -(d.a.track ?? 0), // deck angle is CCW; heading is CW from north
      getColor: (d) => [...(d.mil ? MIL : AMBER), Math.round(d.alpha * 255)],
      getSize: (d) => SIZE_FOR[d.cls],
      sizeUnits: "pixels",
      billboard: true,
      pickable: true,
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
    `<dt>Recv</dt><dd>${esc(a.recv || "—")}</dd>` +
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
    if (j.server_ts < snap.server_ts) return; // monotonic guard: ignore stale snapshots
    snap = { server_ts: j.server_ts, aircraft: j.aircraft || [], perf0: performance.now() / 1000 };

    const total = snap.aircraft.length;
    const milCount = snap.aircraft.filter((a) => a.is_military === true).length;
    document.getElementById("stat-total").textContent = total;
    document.getElementById("stat-mil").textContent = milCount;
    const d = new Date(j.server_ts * 1000);
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
