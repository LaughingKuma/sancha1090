// One shared cell even across cache-skewed module duplicates: two ?v= URLs fork the module,
// and the poller and readers must resolve ONE object or they split-brain for the cache window.
export const S = (globalThis.__sancha_state ??= {
  // Anchor the snapshot to the server clock so dead-reckoning and age never jump on a new poll.
  snap: { server_ts: 0, aircraft: [], perf0: 0 },
  selected: null, // { hex, pts: [{lon, lat, ts, altFt, est}], mil }
  feederCenter: null, // [lon, lat] from /range-outline
  countryIso2: {}, // country NAME → ISO2 (built from flag-icons); empty until loaded
  outlineData: [],
  historyLoaded: false,
  pings: [],
  trailSegments: [],
  selectedSegments: [],
  histPts: [], // fetched historical fused path of a clicked recent-sighting [{lon, lat, ts}]
  histSegments: [], // gap-split segments for the HISTORY path layer (constant muted-slate colour)
  histMarkers: [], // journey endpoints: hollow dot at the start, filled at the end
  histCrumbs: [], // orphan fixes (no segment either side) as small slate dots — keeps a sparse path legible
  histFlightId: null, // flight_id (decimal string) of the drawn sighting; null = none drawn
  histProvisional: false, // drawn sighting's path is serve-time fused, not yet settled in the mart
  trackFetchSeq: 0,
  flightsFetchSeq: 0,
  pathFetchSeq: 0, // orphans an in-flight /path fetch on a newer click or deselect
  trails: new Map(), // hex → { pts, mil }
  renderState: new Map(), // hex → { offset, snapTs, prev, t }
  lastSeen: new Map(), // hex → last capture_ts
  estSegments: [], // dashed estimate overlay segments [{path, kind, band}] — explicit request only (§9)
  estSubjectKey: null, // "f:<fid>"|"h:<hex>" of the DRAWN estimate (log's namespaced form); null = none drawn
  estFetchSeq: 0, // orphans an in-flight /estimate fetch on a newer click or clear — increment-only
  estPendingKey: null, // "f:<fid>"|"h:<hex>" of an in-flight estimate fetch — a second click cancels instead of re-fetching (§9)
  histPathN: 0, // points actually drawn for the history path — the estimate button needs proof, not intent
});

// a dead feed must read as "display stopped", not as a fleet-wide signal-loss event
export const STREAM_FREEZE_S = 3;
export function serverNow() {
  return S.snap.server_ts + Math.min(performance.now() / 1000 - S.snap.perf0, STREAM_FREEZE_S);
}
