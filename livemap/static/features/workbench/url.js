// The URL is the only saved-view store (design §1) — every navigation step is a history entry.
const VIEWS = new Set(["overview", "drill", "log", "flags", "trends"]);
const RANGE_RE = /^(?:7d|30d|90d|all|\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2})$/;
const CLASSES = new Set([
  "tiebreak_endpoint", "single_source", "one_sided_intl", "feasibility_snap", "diversion", "military",
]);
const DIMS = new Set(["route", "airline", "airport"]);
// <icao24>.<start_epoch>, never flight_id: the id churns on every rebuild, the pair does not
const INST_RE = /^[0-9a-f]{6}\.\d{9,11}(\.[a-z0-9]{1,8})?$/i;
// canonical key form: hex lower, callsign UPPER — whole-string lowercasing broke equality with the
// row keys data.js mints (uppercase callsign), unlighting the active row after reload/popstate
const instNorm = (v) => {
  const [h, e, c] = v.split(".");
  return `${h.toLowerCase()}.${e}${c ? `.${c.toUpperCase()}` : ""}`;
};

const str = (v, max) => (typeof v === "string" && v.length && v.length <= max ? v : null);

// The wire scheme is fixed (design §5), so scoped-but-unserialized filters ride the history ENTRY
// instead of the query string — Back then restores the view it left rather than a reset one.
const scopeOf = (st) => ({ hex: st.hex || null, apt: st.apt || null, type: st.type || null });
const scopeKey = (s) => `${s.hex || ""}|${s.apt || ""}|${s.type || ""}`;

export function readUrl(search = location.search) {
  const p = new URLSearchParams(search);
  const view = p.get("wb");
  const range = p.get("wb_d");
  const inst = p.get("wb_inst");
  const sc = (history.state && history.state.wb) || {};
  return {
    view: VIEWS.has(view) ? view : "overview",
    range: range && RANGE_RE.test(range) ? range : "30d",
    airline: str(p.get("wb_airline"), 128), // same cap as data.js NAME_MAX — a shorter one drops the filter on reload
    service: str(p.get("wb_svc"), 16),
    od: str(p.get("wb_od"), 16),
    inst: inst && INST_RE.test(inst) ? instNorm(inst) : null,
    mil: p.get("wb_mil") === "1",
    flagClass: CLASSES.has(p.get("wb_class")) ? p.get("wb_class") : null,
    dim: DIMS.has(p.get("wb_dim")) ? p.get("wb_dim") : "route",
    page: Math.min(Math.max(parseInt(p.get("wb_p"), 10) || 1, 1), 999),
    hex: str(sc.hex, 6),
    apt: str(sc.apt, 4),
    type: str(sc.type, 8),
  };
}

// Only the wb_* namespace is rewritten — ?sel= and other livemap params must survive a navigation.
export function writeUrl(st, replace = false) {
  const p = new URLSearchParams(location.search);
  const set = (k, v) => (v ? p.set(k, String(v)) : p.delete(k));
  set("wb", st.view);
  set("wb_d", st.range);
  set("wb_airline", st.airline);
  set("wb_svc", st.service);
  set("wb_od", st.od);
  set("wb_inst", st.inst);
  set("wb_mil", st.mil ? "1" : "");
  set("wb_class", st.flagClass);
  set("wb_dim", st.dim !== "route" ? st.dim : ""); // route is the default — it needs no URL
  set("wb_p", st.page > 1 ? st.page : "");
  const q = p.toString();
  const url = `${location.pathname}${q ? `?${q}` : ""}${location.hash}`;
  const scope = scopeOf(st);
  // a no-op change must not stack an identical entry the Back button can't escape
  const same =
    url === `${location.pathname}${location.search}${location.hash}` &&
    scopeKey(scope) === scopeKey((history.state && history.state.wb) || {});
  if (replace || same) history.replaceState({ wb: scope }, "", url);
  else history.pushState({ wb: scope }, "", url);
}
