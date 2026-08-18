const DIM = 0.85; // the live fleet recedes to context; the drawn path is the subject
const noop = () => {};

let api = null; // the map facade — the island's only handle on the map
let focus = null; // { inst, n }: claimed before the path lands, so n stays null until it does
let focusSeq = 0; // owns the focus lifecycle; every terminal path checks it
let exitCb = null;
let listener = null;

export function initFocus(mapApi) {
  api = mapApi;
}

export function onFocusChange(cb) {
  listener = cb;
}

const notify = () => listener && listener(focus);

// silent = a newer focus is superseding this one: its exit callback drops wb_inst from the URL,
// which would erase the very deep link being restored.
function teardown(silent) {
  focusSeq++; // orphan any pending focus fetch — its handlers re-check ownership before acting
  if (!focus) return;
  focus = null;
  api.clearPath();
  api.dimLive(0);
  api.guardMapClicks(false);
  notify();
  const cb = exitCb;
  exitCb = null;
  if (!silent && cb) cb();
}

export function enterFocus(inst, opts = {}) {
  const empty = opts.onEmpty || noop;
  if (!inst || !inst.flightId) return empty("no recorded path");
  teardown(true);
  // the spotlight drives this same path pipeline and its rows sit outside #map's click guard —
  // dropping the live selection keeps one pivot, so nothing can overwrite the focused path
  api.clearSelection();
  const my = ++focusSeq;
  // Ownership is claimed BEFORE the path lands: dim + guard + bar land now, so Esc/Back can cancel a
  // pending focus and a live-aircraft pick can no longer race the request in flight.
  api.dimLive(DIM);
  api.guardMapClicks(true);
  focus = { inst, n: null };
  exitCb = opts.onExit || null;
  notify();
  api.showFlightPath(inst.flightId, { fit: true }).then(({ status, n }) => {
    if (my !== focusSeq || status === "superseded") return; // a stale answer never tears down a newer focus
    if (status === "ok") {
      focus.n = n;
      return notify();
    }
    teardown(true);
    empty(status === "failed" ? "path unavailable" : "no recorded path");
  });
}

export function exitFocus() {
  teardown(false);
}

// silent teardown for supersession: no exit callback, so the incoming deep link's URL survives
export function dropFocus() {
  teardown(true);
}

export const isFocused = () => focus !== null;
export const focusedKey = () => focus?.inst.key ?? null;
