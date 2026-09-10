// The map's one public handle: it owns the shared /path pipeline, the fleet dim and the map click
// guard, so a feature island can drive the map without ever reaching into the state cell.

/**
 * @param {import("../src/map/facade").FacadeDeps} deps
 * @returns {import("../src/map/facade").MapFacade}
 */
export function createMapFacade({ S, map, setHistPath, clearHistPath, clearSelection }) {
  /** @type {AbortController | null} */
  let inflight = null;

  // Frame the whole journey unless both ends are already on-screen. Endpoints, not a point-count fraction:
  // dense approach fixes cluster at one end and would fool a fraction test on a trans-ocean flight.
  /** @param {import("../src/map/facade").PathPoint[]} pts */
  function fitPath(pts) {
    if (pts.length < 2) return;
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const p of pts) { w = Math.min(w, p.lon); e = Math.max(e, p.lon); s = Math.min(s, p.lat); n = Math.max(n, p.lat); }
    // antimeridian: a naive lon box wider than 180° is the wrong way round the globe — shift western-hemisphere
    // lons +360 so the box wraps the dateline the short way (fitBounds accepts lngs > 180). HNL legs hit this.
    if (e - w > 180) {
      w = Infinity; e = -Infinity;
      for (const p of pts) { const lon = p.lon < 0 ? p.lon + 360 : p.lon; w = Math.min(w, lon); e = Math.max(e, lon); }
    }
    const b = map.getBounds();
    const inView = (/** @type {import("../src/map/facade").PathPoint} */ p) => p.lon >= b.getWest() && p.lon <= b.getEast() && p.lat >= b.getSouth() && p.lat <= b.getNorth();
    // maplibre honours prefers-reduced-motion, so the flight is instant for those users
    if (!(inView(pts[0]) && inView(pts[pts.length - 1])))
      map.fitBounds([[w, s], [e, n]], { padding: 80, maxZoom: 11, duration: 700 });
  }

  // One claim for both entry points: abort any in-flight fetch (a ~1 MB settled path must not download and
  // parse for nothing) and drop the drawn path now, so a request and a clear are the same operation.
  function clearPath() {
    S.pathFetchSeq++;
    inflight?.abort();
    clearHistPath();
    S.histPathN = 0;
  }

  return {
    async showFlightPath(flightId, { fit = false } = {}) {
      clearPath(); // synchronous: any request supersedes an in-flight one, spotlight or focus alike
      const seq = S.pathFetchSeq;
      const ctl = new AbortController();
      inflight = ctl;
      let j = null;
      try {
        const r = await fetch(`/path/${encodeURIComponent(flightId)}`, { cache: "no-store", signal: ctl.signal });
        j = r.ok ? await r.json() : null;
      } catch (e) {
        if (e instanceof Error && e.name === "AbortError") return { status: "superseded", n: 0 }; // clearPath cut this one loose
      }
      if (seq !== S.pathFetchSeq) return { status: "superseded", n: 0 }; // a newer owner holds the pipeline
      let n;
      try {
        if (!Array.isArray(j?.points)) throw new TypeError("points"); // a malformed body is a failure, not an empty path
        n = setHistPath(j.points);
      } catch {
        return { status: "failed", n: 0 };
      }
      S.histPathN = n;
      S.histProvisional = !!j.provisional && n > 0; // an empty provisional draws nothing — no badge either
      if (!n) return { status: "empty", n: 0 };
      // framing is best-effort: a map error must not undraw a path that landed
      if (fit) try { fitPath(S.histPts); } catch { /* keep the drawn path */ }
      return { status: "ok", n };
    },
    clearPath,
    dimLive(x) {
      S.dimLive = x;
    },
    clearSelection,
    // A flag the map's own click handler consults, not a DOM swallow: a capture-phase listener on #map
    // also starved the zoom buttons inside it, and the guard is about the selection, not the DOM.
    guardMapClicks(on) {
      S.mapClickGuard = !!on;
    },
  };
}
