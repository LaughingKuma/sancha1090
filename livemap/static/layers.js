import { S, serverNow } from "./state.js";
import { RING_NM, AIRPORTS, RUNWAY_PATHS, RUNWAY_ENDS, AMBER, MIL, TEAL, HISTORY } from "./constants.js";
import { SIL, CHEV_UP, CHEV_DOWN, zoomMult } from "./silhouettes.js";
import { LABEL_ZOOM, LABEL_MAX, labelText, shadowPx, SHADOW_DIR } from "./altitude.js";
import { frameData, metresBetween } from "./motion.js";
import { emergencyOf } from "./telemetry.js";
import { map, overlay } from "./mapsetup.js";

const { IconLayer, ScatterplotLayer, PolygonLayer, PathLayer, TextLayer, PathStyleExtension } = deck;

// deck layers can't read CSS media queries — gate the emergency pulse on the OS preference here.
// typeof guard: a non-browser import (e.g. jsdom) lacks matchMedia → default to animated.
const REDUCED_MOTION =
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

// Ring only when a hex is NEW or silent >10 s — per-fix pings (1 Hz) would be constant static.
const PING_GAP_S = 10;
const PING_LIFE_S = 1.2;
export function detectAcquisitions() {
  const t = serverNow();
  for (const a of S.snap.aircraft) {
    if (!a.hex || a.lon == null || a.lat == null) continue;
    const ct = Number(a.capture_ts);
    if (!Number.isFinite(ct)) continue;
    const prev = S.lastSeen.get(a.hex);
    if (prev === undefined || ct - prev > PING_GAP_S)
      S.pings.push({ pos: [a.lon, a.lat], t0: t, mil: a.is_military === true });
    S.lastSeen.set(a.hex, ct);
  }
  for (const [hex, ts] of S.lastSeen) if (t - ts > 600) S.lastSeen.delete(hex); // bound memory
}

// country NAME → ISO2 (built from flag-icons); drives the card flag chip. Empty until loaded.
(async () => {
  try {
    const r = await fetch("country-iso2.json");
    if (r.ok) S.countryIso2 = await r.json();
  } catch {} // no map → no flags, never an error
})();

// Receiver coverage outline + dot — slow-changing, fetched separately from the 1 Hz aircraft poll.
async function loadOutline() {
  try {
    const j = await (await fetch("/range-outline", { cache: "no-store" })).json();
    S.feederCenter = j.center || null;
    S.outlineData = j.ring && j.ring.length ? [{ ring: j.ring }] : [];
  } catch (e) {
    /* outline is optional — absent until the batch job has run */
  }
}
loadOutline();
setInterval(loadOutline, 300000);

function buildLayers() {
  const tNow = serverNow();
  S.pings = S.pings.filter((p) => tNow - p.t0 < PING_LIFE_S);
  // crowded frame → demand one more zoom level before labels appear
  const labelZoom = LABEL_ZOOM + (S.snap.aircraft.length > LABEL_MAX ? 1 : 0);
  const showLabels = map.getZoom() >= labelZoom;
  const data = frameData(zoomMult(map.getZoom()));
  const emergencyData = data.filter((d) => emergencyOf(d.a));
  // free-running pulse (independent of poll freshness); frozen under reduced-motion
  const pulsePhase = REDUCED_MOTION ? 0 : ((performance.now() / 1000) % 1.8) / 1.8;
  // elastic band: the wake terminates at the rendered icon in every state (tar1090's rule);
  // dimmer than recorded track, and suppressed mid-ease so it never sweeps the coverage hole
  const bridges = [];
  for (const d of data) {
    const tr = S.trails.get(d.a.hex);
    const head = tr && tr.pts[tr.pts.length - 1];
    if (!head) continue;
    const st = S.renderState.get(d.a.hex);
    if (st && metresBetween(st.offset[0], st.offset[1], d.pos[1]) > 400) continue;
    if (head.lon !== d.pos[0] || head.lat !== d.pos[1])
      bridges.push({ path: [[head.lon, head.lat], d.pos], color: [...d.tint, Math.round(87 * d.alpha)] });
  }
  const ringData = S.feederCenter ? RING_NM.map((nm) => ({ nm })) : [];
  return [
    // station range rings — beneath everything; fresh data array each frame so a late
    // feederCenter fetch is picked up (deck only recomputes attributes on data change)
    new ScatterplotLayer({
      id: "range-rings",
      data: ringData,
      getPosition: () => S.feederCenter,
      getRadius: (d) => d.nm * 1852,
      radiusUnits: "meters",
      stroked: true,
      filled: false,
      getLineColor: [...TEAL, 90],
      getLineWidth: 1,
      lineWidthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    new TextLayer({
      id: "range-ring-labels",
      data: ringData,
      getPosition: (d) => [S.feederCenter[0], S.feederCenter[1] + d.nm / 60], // 1 nm = 1/60° lat
      getText: (d) => `${d.nm} nm`,
      getSize: 10,
      getColor: [...TEAL, 150],
      fontFamily: "'Spline Sans Mono', monospace",
      getTextAnchor: "middle",
      getAlignmentBaseline: "bottom",
      parameters: { depthTest: false },
    }),
    // airport furniture — above rings, below everything live
    new PathLayer({
      id: "airport-runways",
      data: RUNWAY_PATHS,
      getPath: (d) => d.path,
      getColor: [...TEAL, 140], // ring teal — slate vanished against the basemap's pale strips
      getWidth: 3,
      widthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    new TextLayer({
      id: "airport-codes",
      data: AIRPORTS,
      getPosition: (d) => d.label,
      getText: (d) => d.code,
      getSize: 9,
      getColor: [...TEAL, 150],
      fontFamily: "'Spline Sans Mono', monospace",
      getTextAnchor: "middle",
      getAlignmentBaseline: "top",
      parameters: { depthTest: false },
    }),
    new TextLayer({
      id: "runway-names",
      data: map.getZoom() >= 11.2 ? RUNWAY_ENDS : [], // designators only once the basemap strips render
      getPosition: (d) => d.pos,
      getText: (d) => d.text,
      getSize: 8,
      getColor: [...TEAL, 170],
      getPixelOffset: (d) => d.off,
      fontFamily: "'Spline Sans Mono', monospace",
      getTextAnchor: "middle",
      getAlignmentBaseline: "center",
      billboard: true,
      parameters: { depthTest: false },
    }),
    // coverage polygon, beneath everything — terrain-shaped reception envelope
    new PolygonLayer({
      id: "range-outline",
      data: S.outlineData,
      getPolygon: (d) => d.ring,
      stroked: true,
      filled: true,
      getFillColor: [24, 116, 130, 20],
      getLineColor: [...TEAL, 130],
      getLineWidth: 1.3,
      lineWidthUnits: "pixels",
      parameters: { depthTest: false },
    }),
    // clicked recent-sighting's historical fused path (fct_flight_path) — a constant muted-slate dashed ghost,
    // recessive under the live amber trail so a past journey reads as its own class at a glance
    new PathLayer({
      id: "history-path",
      data: S.histSegments,
      getPath: (d) => d.path,
      getColor: [...HISTORY, 150],
      getWidth: 2,
      widthUnits: "pixels",
      capRounded: true,
      getDashArray: [4, 6], // longer dash than any live layer — dotted-ghost reading, never confused with a wake
      wrapLongitude: true, // a dateline-crossing segment (adjacent fixes at ±179.9°) must wrap the short way
      extensions: [new PathStyleExtension({ dash: true })],
      parameters: { depthTest: false },
    }),
    // orphan fixes (no segment either side) as small slate breadcrumbs — an all-sparse path still reads as a
    // dotted trail, not two lone markers; clearly smaller than the 5px endpoints so start/end still dominate
    new ScatterplotLayer({
      id: "history-crumbs",
      data: S.histCrumbs,
      getPosition: (d) => d.pos,
      getRadius: 2.5,
      radiusUnits: "pixels",
      stroked: false,
      filled: true,
      getFillColor: [...HISTORY, 170],
      wrapLongitude: true,
      parameters: { depthTest: false },
    }),
    // its endpoints — hollow slate dot at the start, filled at the end, so the trajectory reads as a journey
    new ScatterplotLayer({
      id: "history-endpoints",
      data: S.histMarkers,
      getPosition: (d) => d.pos,
      getRadius: 5,
      radiusUnits: "pixels",
      stroked: true,
      filled: true,
      getFillColor: (d) => (d.filled ? [...HISTORY, 235] : [0, 0, 0, 0]), // filled arrival vs truly-hollow start (transparent, not void-fill)
      getLineColor: [...HISTORY, 235],
      getLineWidth: 1.5,
      lineWidthUnits: "pixels",
      wrapLongitude: true,
      parameters: { depthTest: false },
    }),
    // selected aircraft's 30-min track — under the wake so the live fade reads on top
    new PathLayer({
      id: "selected-track",
      data: S.selectedSegments,
      getPath: (d) => d.path,
      getColor: (d) => d.color,
      getWidth: 2.2,
      widthUnits: "pixels",
      capRounded: true,
      getDashArray: (d) => d.dash,
      extensions: [new PathStyleExtension({ dash: true })],
      parameters: { depthTest: false },
    }),
    // fading wake of real fixes — approach streams into HND/NRT read as structure, not dots
    new PathLayer({
      id: "trails",
      data: S.trailSegments,
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
      data: S.pings,
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
      getRadius: (d) => Math.max(d.mil ? 15 : 8, d.size * (d.mil ? 0.62 : 0.55)), // disc a touch wider than the airframe; mil keeps the wider burn
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
      getSize: (d) => d.size + 4, // constant 2px rim — proportional 1.22x read as bare wingtips at v5.6 sizes
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
      data: S.feederCenter ? [S.feederCenter] : [],
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
    // PR 1b-i: always-on red pulse on emergency-squawk contacts — topmost so it's never occluded
    // by the icon it alerts on; static ring under reduced-motion.
    new ScatterplotLayer({
      id: "emergency-pulse",
      data: emergencyData,
      getPosition: (d) => d.pos,
      getRadius: REDUCED_MOTION ? 17 : 9 + 21 * pulsePhase, // constant accessor → animates as a uniform, no per-row upload
      radiusUnits: "pixels",
      stroked: true,
      filled: false,
      getLineColor: REDUCED_MOTION ? [...MIL, 210] : [...MIL, Math.round(210 * (1 - pulsePhase))],
      getLineWidth: 2,
      lineWidthUnits: "pixels",
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
