"use strict";

const { MapboxOverlay, IconLayer, ScatterplotLayer } = deck;

// 60 s is the MV's data contract — fade-by-age visually recovers freshness within it.
const WINDOW_S = 60;
const AMBER = [255, 176, 0];
const MIL = [255, 59, 48];
const KT_TO_MS = 0.514444;

// North-pointing silhouette, baked as an SVG data-URI; mask:true lets deck tint it per-aircraft.
const PLANE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">' +
  '<path fill="#fff" d="M32 3c-2.4 0-3.7 3-3.9 8.6l-.3 11.2-22.5 13.3c-.8.5-1.3 1.4-1.3 2.4v3.3' +
  'c0 .9.9 1.6 1.8 1.3l22-6.6.4 12.6-6.7 5c-.5.4-.8 1-.8 1.6v2.4c0 .8.7 1.4 1.5 1.2l10.2-2.7' +
  '10.2 2.7c.8.2 1.5-.4 1.5-1.2v-2.4c0-.6-.3-1.2-.8-1.6l-6.7-5 .4-12.6 22 6.6c.9.3 1.8-.4 1.8-1.3' +
  'v-3.3c0-1-.5-1.9-1.3-2.4L36.2 22.8l-.3-11.2C35.7 6 34.4 3 32 3z"/></svg>';
const ICON = {
  url: "data:image/svg+xml;charset=utf-8," + encodeURIComponent(PLANE_SVG),
  width: 64, height: 64, anchorX: 32, anchorY: 32, mask: true,
};

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
  const dist = a.gs * KT_TO_MS * age; // metres flown since the fix
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
    return { a, pos: deadReckon(a, age), alpha, mil: a.is_military === true };
  });
}

function buildLayers() {
  const data = frameData();
  return [
    // Soft phosphor glow under each contact — military burns hotter and wider.
    new ScatterplotLayer({
      id: "glow",
      data,
      getPosition: (d) => d.pos,
      getRadius: (d) => (d.mil ? 15 : 9),
      radiusUnits: "pixels",
      getFillColor: (d) => [...(d.mil ? MIL : AMBER), Math.round(d.alpha * (d.mil ? 130 : 70))],
      stroked: false,
      parameters: { depthTest: false },
    }),
    new IconLayer({
      id: "planes",
      data,
      getIcon: () => ICON,
      getPosition: (d) => d.pos,
      getAngle: (d) => -(d.a.track ?? 0), // deck angle is CCW; heading is CW from north
      getColor: (d) => [...(d.mil ? MIL : AMBER), Math.round(d.alpha * 255)],
      getSize: (d) => (d.mil ? 26 : 21),
      sizeUnits: "pixels",
      billboard: true,
      pickable: true,
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
  const html =
    `<div class="flight ${mil ? "mil" : ""}">${esc(a.flight || a.hex || "UNKNOWN")}${badges}</div>` +
    `<div class="org">${esc(a.airline_name || "Unregistered callsign")}</div>` +
    "<dl>" +
    `<dt>ICAO</dt><dd>${esc((a.hex || "—").toUpperCase())}</dd>` +
    `<dt>Origin</dt><dd>${esc(a.reg_country || "—")}</dd>` +
    `<dt>Alt</dt><dd>${esc(alt)}</dd>` +
    `<dt>Speed</dt><dd>${esc(spd)}</dd>` +
    `<dt>Heading</dt><dd>${esc(hdg)}</dd>` +
    `<dt>Recv</dt><dd>${esc(a.recv || "—")}</dd>` +
    "</dl>";
  return { html, className: "ac-tip" };
}

// ── Poll the server-side cache (1 query/s shared across every tab) ──
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
      `Synced ${fmt(d.getHours())}:${fmt(d.getMinutes())}:${fmt(d.getSeconds())} · ${total} contacts · 1 query/s`;
  } catch (e) {
    document.getElementById("meta-line").textContent = `Stream error — retrying… (${e.message})`;
  } finally {
    pollInFlight = false;
  }
}
poll();
setInterval(poll, 1000);
