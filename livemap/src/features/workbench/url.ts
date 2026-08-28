// The URL is the only saved-view store (design §1): every navigation step is a history entry.
// Each vocabulary is spelled once — the array validates at runtime and derives the union type.
export const VIEW_NAMES = ["overview", "drill", "log", "flags", "trends", "estimates", "coverage"] as const;
export type ViewName = (typeof VIEW_NAMES)[number];
export const DIMS = ["route", "airline", "airport"] as const;
export type Dim = (typeof DIMS)[number];
export const FLAG_CLASSES = [
  "tiebreak_endpoint", "single_source", "one_sided_intl", "feasibility_snap", "diversion",
  "same_endpoint", "military",
] as const;
export type FlagClass = (typeof FLAG_CLASSES)[number];

export interface WbState {
  view: ViewName;
  range: string;
  airline: string | null;
  service: string | null;
  od: string | null;
  inst: string | null;
  mil: boolean;
  flagClass: FlagClass | null;
  dim: Dim;
  page: number;
  hex: string | null;
  apt: string | null;
  type: string | null;
}

export const RANGE_PRESETS: Record<string, number> = { "7d": 7, "30d": 30, "90d": 90 };
export const CUSTOM_RANGE_RE = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/;
const VIEW_SET: ReadonlySet<string> = new Set(VIEW_NAMES);
const CLASS_SET: ReadonlySet<string> = new Set(FLAG_CLASSES);
const DIM_SET: ReadonlySet<string> = new Set(DIMS);
const RANGE_SET: ReadonlySet<string> = new Set(Object.keys(RANGE_PRESETS));
const validRange = (r: string) => r === "all" || RANGE_SET.has(r) || CUSTOM_RANGE_RE.test(r);

// The one spelling of every fallback: readUrl merges validated params over these, doorway resets pick from them.
export const DEFAULTS: WbState = {
  view: "overview", range: "30d", airline: null, service: null, od: null, inst: null, mil: false,
  flagClass: null, dim: "route", page: 1, hex: null, apt: null, type: null,
};
// <icao24>.<start_epoch>, never flight_id: the id churns on every rebuild, the pair does not
const INST_RE = /^[0-9a-f]{6}\.\d{9,11}(\.[a-z0-9]{1,8})?$/i;
// canonical key form: hex lower, callsign UPPER — whole-string lowercasing broke equality with the
// row keys data.ts mints (uppercase callsign), unlighting the active row after reload/popstate
const instNorm = (v: string) => {
  const [h, e, c] = v.split(".");
  return `${h.toLowerCase()}.${e}${c ? `.${c.toUpperCase()}` : ""}`;
};

const str = (v: string | null, max: number) => (typeof v === "string" && v.length && v.length <= max ? v : null);

interface ScopeState { hex?: string | null; apt?: string | null; type?: string | null }

export function readUrl(search: string = location.search): WbState {
  const p = new URLSearchParams(search);
  const view = p.get("wb");
  const range = p.get("wb_d");
  const inst = p.get("wb_inst");
  const sc: ScopeState = (history.state && history.state.wb) || {};
  const cls = p.get("wb_class");
  const dim = p.get("wb_dim");
  return {
    view: (VIEW_SET.has(view ?? "") ? view : DEFAULTS.view) as ViewName,
    range: range && validRange(range) ? range : DEFAULTS.range,
    airline: str(p.get("wb_airline"), 128), // same cap as data.ts NAME_MAX — a shorter one drops the filter on reload
    service: str(p.get("wb_svc"), 16),
    od: str(p.get("wb_od"), 16),
    inst: inst && INST_RE.test(inst) ? instNorm(inst) : null,
    mil: p.get("wb_mil") === "1",
    flagClass: (CLASS_SET.has(cls ?? "") ? cls : null) as FlagClass | null,
    dim: (DIM_SET.has(dim ?? "") ? dim : DEFAULTS.dim) as Dim,
    page: Math.min(Math.max(parseInt(p.get("wb_p") ?? "", 10) || 1, 1), 999),
    hex: str(sc.hex ?? null, 6),
    apt: str(sc.apt ?? null, 4),
    type: str(sc.type ?? null, 8),
  };
}

// Only the wb_* namespace is rewritten — ?sel= and other livemap params must survive a navigation.
export function writeUrl(st: WbState, replace = false) {
  const p = new URLSearchParams(location.search);
  const set = (k: string, v: string | number | null) => (v ? p.set(k, String(v)) : p.delete(k));
  set("wb", st.view);
  set("wb_d", st.range);
  set("wb_airline", st.airline);
  set("wb_svc", st.service);
  set("wb_od", st.od);
  set("wb_inst", st.inst);
  set("wb_mil", st.mil ? "1" : "");
  set("wb_class", st.flagClass);
  set("wb_dim", st.dim !== DEFAULTS.dim ? st.dim : ""); // the default dim needs no URL
  set("wb_p", st.page > 1 ? st.page : "");
  const q = p.toString();
  const url = `${location.pathname}${q ? `?${q}` : ""}${location.hash}`;
  const scope: ScopeState = { hex: st.hex || null, apt: st.apt || null, type: st.type || null };
  const scopeKey = (s: ScopeState) => `${s.hex || ""}|${s.apt || ""}|${s.type || ""}`;
  // a no-op change must not stack an identical entry the Back button can't escape
  const same =
    url === `${location.pathname}${location.search}${location.hash}` &&
    scopeKey(scope) === scopeKey((history.state && history.state.wb) || {});
  if (replace || same) history.replaceState({ wb: scope }, "", url);
  else history.pushState({ wb: scope }, "", url);
}
