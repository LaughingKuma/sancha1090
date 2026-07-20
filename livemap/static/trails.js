import { S, serverNow } from "./state.js";
import { MIL, MAX_DR_S } from "./constants.js";
import { parseAlt, altTint } from "./altitude.js";

// Last ~90 s of real fixes per hex, accumulated client-side from the poll — zero backend.
const TRAIL_S = 90;
const TRAIL_GAP_S = 2; // sub-2s fixes add segments without adding visible shape
const GAP_EST_S = MAX_DR_S; // a gap the DR envelope couldn't cover on screen is estimated, not flown track
export function pushFix(pts, lon, lat, ts, altRaw) {
  const t = Number(ts);
  if (lon == null || lat == null || !Number.isFinite(t)) return false;
  const last = pts[pts.length - 1];
  if (!last || (t - last.ts >= TRAIL_GAP_S && (lon !== last.lon || lat !== last.lat))) {
    pts.push({ lon, lat, ts: t, altFt: parseAlt(altRaw), est: !!last && t - last.ts > GAP_EST_S });
    return true;
  }
  return false;
}
export function ingestTrails(rows = S.snap.aircraft) {
  for (const a of rows) {
    if (!a.hex || a.lon == null || a.lat == null || a.capture_ts == null) continue;
    let tr = S.trails.get(a.hex);
    if (!tr) S.trails.set(a.hex, (tr = { pts: [], mil: false }));
    tr.mil = a.is_military === true;
    pushFix(tr.pts, a.lon, a.lat, a.capture_ts, a.alt_baro);
  }
}
// Rebuilt on its own clock so trails keep fading through stream errors; 1 Hz is invisible
// at a 90 s fade and far cheaper than rebuilding per frame.
export function rebuildTrailSegments() {
  const t = serverNow();
  const segs = [];
  for (const [, tr] of S.trails) {
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
  S.trailSegments = segs;
}
setInterval(rebuildTrailSegments, 1000);

// One-shot /history replay through the existing ingest dedup — a fresh tab starts
// with the 90 s wakes already drawn instead of accumulating from zero.
export async function loadHistory() {
  S.historyLoaded = true; // set before the await so the 500 ms poll can't double-fire the fetch
  try {
    const r = await fetch("/history?s=90", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    const live = new Set(S.snap.aircraft.map((a) => a.hex));
    S.trails.clear();
    for (const [, rows] of j.snapshots || []) {
      ingestTrails(
        rows
          .filter((r) => live.has(r[0])) // expired hexes must not flash orphan wakes
          .map(([hex, lon, lat, capture_ts, alt_baro]) => ({ hex, lon, lat, capture_ts, alt_baro })),
      );
    }
    ingestTrails(); // re-ingest the live snapshot so wake heads end on the newest real fix
    rebuildTrailSegments();
  } catch (e) {
    S.historyLoaded = false; // transient failure must not forfeit the backfill — retry next poll
  }
}

// One aircraft at most; its 30-min track comes from /track on click and grows live from the poll.
const SELECTED_TRACK_S = 1800; // mirror the sidecar buffer window — live growth must not exceed it

// hours-long selections must not accumulate unbounded geometry
export function pruneSelectedPts() {
  const cutoff = S.selected.pts[S.selected.pts.length - 1].ts - SELECTED_TRACK_S;
  while (S.selected.pts.length > 1 && S.selected.pts[0].ts < cutoff) S.selected.pts.shift();
}

export function rebuildSelectedSegments() {
  const segs = [];
  if (S.selected) {
    for (let i = 1; i < S.selected.pts.length; i++) {
      const p = S.selected.pts[i];
      segs.push({
        path: [[S.selected.pts[i - 1].lon, S.selected.pts[i - 1].lat], [p.lon, p.lat]],
        // constant alpha — the point of the track is seeing the old parts, so no age fade
        color: [...(S.selected.mil ? MIL : altTint(p.altFt)), Math.round(200 * (p.est ? 0.35 : 1))],
        dash: p.est ? [6, 4] : [0, 0],
      });
    }
  }
  S.selectedSegments = segs;
}

export function appendSelectedFix(a) {
  S.selected.mil = a.is_military === true;
  if (pushFix(S.selected.pts, a.lon, a.lat, a.capture_ts, a.alt_baro)) {
    pruneSelectedPts();
    rebuildSelectedSegments();
  }
}

// A clicked recent-sighting's historical fused path (fct_flight_path). The mart is per-second with real
// coverage holes, so split on gaps > 60 s — a hole must read as a hole, never a straight line drawn as if flown.
// Colour is a constant muted slate (set on the layer, not per-fix): a past journey is one class, not altitude.
const HIST_GAP_S = 60;
export function rebuildHistSegments() {
  const segs = [];
  const crumbs = [];
  const pts = S.histPts;
  for (let i = 1; i < pts.length; i++) {
    if (pts[i].ts - pts[i - 1].ts > HIST_GAP_S) continue; // gap → leave the hole empty, never bridge it
    segs.push({ path: [[pts[i - 1].lon, pts[i - 1].lat], [pts[i].lon, pts[i].lat]] });
  }
  // orphan fixes — no segment on either side — become breadcrumb dots so an all-sparse path (e.g. a
  // 5-fix opensky-only trace, all gaps > 60 s) reads as a dotted trail, not two lone markers. Interior only:
  // the endpoints already carry the hollow/filled markers. Never a connecting line — the honesty rule stands.
  for (let i = 1; i < pts.length - 1; i++) {
    if (pts[i].ts - pts[i - 1].ts > HIST_GAP_S && pts[i + 1].ts - pts[i].ts > HIST_GAP_S)
      crumbs.push({ pos: [pts[i].lon, pts[i].lat] });
  }
  S.histSegments = segs;
  S.histCrumbs = crumbs;
  // endpoint markers read the trajectory as a completed journey: hollow at the start, filled at the end
  if (!pts.length) S.histMarkers = [];
  else if (pts.length === 1) S.histMarkers = [{ pos: [pts[0].lon, pts[0].lat], filled: true }];
  else S.histMarkers = [
    { pos: [pts[0].lon, pts[0].lat], filled: false },
    { pos: [pts[pts.length - 1].lon, pts[pts.length - 1].lat], filled: true },
  ];
}

// /path returns [lon, lat, ts_epoch, alt_ft, source]; only geometry + time is used (colour is constant).
export function setHistPath(rawPoints) {
  S.histProvisional = false; // geometry replaced/cleared — only the new fetch's response re-arms the flag
  const pts = [];
  for (const [lon, lat, ts] of rawPoints || []) {
    const t = Number(ts);
    // ts == null guard is load-bearing: Number(null) is 0, which passes Number.isFinite and would fake an epoch-0 fix
    if (lon == null || lat == null || ts == null || !Number.isFinite(t)) continue;
    pts.push({ lon, lat, ts: t });
  }
  S.histPts = pts;
  rebuildHistSegments();
  return pts.length;
}

export function clearHistPath() {
  S.histFlightId = null;
  S.histProvisional = false;
  S.histPts = [];
  S.histSegments = [];
  S.histMarkers = [];
  S.histCrumbs = [];
}
