// Single mutable cell shared across modules — importers reassign S.* (legal: property write
// on a shared object), never the import binding itself (illegal across ES modules).
export const S = {
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
  trackFetchSeq: 0,
  trails: new Map(), // hex → { pts, mil }
  renderState: new Map(), // hex → { offset, snapTs, prev, t }
  lastSeen: new Map(), // hex → last capture_ts
};

// a dead feed must read as "display stopped", not as a fleet-wide signal-loss event
export const STREAM_FREEZE_S = 3;
export function serverNow() {
  return S.snap.server_ts + Math.min(performance.now() / 1000 - S.snap.perf0, STREAM_FREEZE_S);
}
