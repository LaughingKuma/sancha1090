// The map's one public handle: it owns the shared /path pipeline, the fleet dim and the #map click
// guard, so a feature island can drive the map without ever reaching into the state cell.

/**
 * @param {import("../src/map/facade").FacadeDeps} deps
 * @returns {import("../src/map/facade").MapFacade}
 */
export function createMapFacade({ S, map, mapEl, setHistPath, clearHistPath, clearSelection }) {
  /** @type {((e: Event) => void) | null} */
  let clickGuard = null;

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

  // One claim for both entry points: orphan any in-flight fetch and drop the drawn path now, so a request
  // and a clear are the same operation — the request merely fills the path back in when its answer wins.
  function clearPath() {
    S.pathFetchSeq++;
    clearHistPath();
    S.histPathN = 0;
  }

  return {
    async showFlightPath(flightId, { fit = false } = {}) {
      clearPath(); // synchronous: any request supersedes an in-flight one, spotlight or focus alike
      const seq = S.pathFetchSeq;
      const j = await fetch(`/path/${encodeURIComponent(flightId)}`, { cache: "no-store" })
        .then((r) => (r.ok ? r.json() : null))
        .catch(() => null);
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
    // Live picking is off while dimmed, so a bare map click reaches the spotlight's clear handler and
    // would wipe the drawn path out from under the caller — swallow it before maplibre dispatches.
    guardMapClicks(on) {
      if (!mapEl) return;
      if (on && !clickGuard) {
        clickGuard = (e) => e.stopPropagation();
        mapEl.addEventListener("click", clickGuard, true);
      } else if (!on && clickGuard) {
        mapEl.removeEventListener("click", clickGuard, true);
        clickGuard = null;
      }
    },
  };
}
