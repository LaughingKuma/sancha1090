import type { Counts, DayCount, DayCounts, GapBin, MixRow, Observed, OdChip, Tier } from "./wire";
import { FLAG_CLASSES } from "./url";
import { csNorm } from "./resolve";

// the reader's view of the wire's DayErr: the envelope promises a float, but a malformed one stays null
// here rather than becoming a fake 0 km
export type EstDay = [string, number | null, number];

// One increment-only sequence per endpoint (the trackFetchSeq pattern) so a stale response never lands;
// "deeplink" is a second instances lane, since a wb_inst resolve runs alongside the view's own list fetch.
const seq = {
  airlines: 0, services: 0, instances: 0, deeplink: 0, search: 0, summary: 0, trends: 0, flags: 0,
  estimates: 0, coverage: 0,
};
type Lane = keyof typeof seq;

// the wire is untrusted JSON: every field is coerced below, never read as a typed value
type Raw = Record<string, unknown>;
const obj = (v: unknown): Raw => (v && typeof v === "object" ? (v as Raw) : {});
const list = (v: unknown): Raw[] => (Array.isArray(v) ? (v as Raw[]) : []);

export interface Airport { icao: string; iata: string; city: string }
export interface Instance {
  hex: string; day: string; startTs: number | null; endTs: number | null; callsign: string;
  airline: string; reg: string; type: string; origin: Airport; dest: Airport; o: string; d: string;
  tier: Tier; gapS: number | null; nPoints: number | null; mil: boolean;
  flightId: string | null; key: string | null;
}
export interface FlagInstance extends Instance { flagClass: string; detail: string }
export interface Paged<T> { rows: T[]; total: number; limit: number; offset: number }
export interface InstancePage extends Paged<Instance> { od: OdChip[]; milAvailable: boolean }
export interface AirlineRow {
  name: string; nFlights: number; nServices: number; firstDay: string; lastDay: string; tiers: Counts;
}
export interface ServiceRow {
  callsign: string; nInstances: number; topOd: OdChip[]; firstDay: string; lastDay: string; tiers: Counts;
}
export interface Mover { key: string; n: number; prevN: number; deltaPct: number | null }
export interface Summary {
  flights: number; aircraft: number; services: number; daily: DayCount[];
  flags: { available: boolean; flagged: number; classes: Counts };
  tiers: { available: boolean; mix: Counts; daily: DayCounts[] };
  est: { available: boolean; errP50Km: number | null; n: number; daily: EstDay[] };
  movers: Mover[];
}
export interface RankRow extends Mover { distinctAircraft: number }
export interface Trends extends Omit<Paged<RankRow>, "rows"> {
  dim: string; series: { key: string; points: DayCount[] }[]; rank: RankRow[];
}
export interface FlagsPage extends Paged<FlagInstance> { available: boolean; classes: Counts }
export interface Estimates {
  available: boolean;
  headline: { configHash: string; n: number; p50Km: number | null; p90Km: number | null; firstDay: string; lastDay: string }[];
  daily: { day: string; configHash: string; p50Km: number | null; p90Km: number | null; n: number }[];
  mix: { available: boolean } & Record<string, MixRow[] | boolean>;
  outcomes: { settled: number; awaiting: number; ambiguous: number };
  inputSplit: { provisional: number; settled: number };
}
export interface Coverage {
  available: boolean; tierDaily: DayCounts[]; gapBins: GapBin[]; observed: Observed[];
}
export interface SearchResult {
  airlines: { name: string; n: number }[];
  services: { callsign: string; airline: string; n: number }[];
  airframes: { hex: string; reg: string; type: string; n: number }[];
  airports: { icao: string; iata: string; name: string; city: string }[];
}

const qs = (o: Record<string, unknown>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o))
    if (v !== null && v !== undefined && v !== "" && v !== false) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

async function call(kind: Lane, path: string, signal?: AbortSignal): Promise<Raw | null> {
  const mine = ++seq[kind];
  try {
    const r = await fetch(path, { cache: "no-store", signal });
    if (!r.ok || mine !== seq[kind]) return null;
    const j = await r.json();
    return mine === seq[kind] ? obj(j) : null; // superseded while the body was parsing
  } catch {
    return null; // an unreachable endpoint renders as empty, never as a broken rail
  }
}

const num = (v: unknown) => (v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
const text = (v: unknown, max = 64) => (v == null ? "" : String(v).slice(0, max));
// same normalization the deep-link comparator applies (resolve.ts) — minted keys and wb_inst must stay equal
const csKeyOf = (c: unknown) => csNorm(c).slice(0, 8);
// airline_name is a filter VALUE (exact match server-side), not just a label — a display-width cap
// would silently drill into nothing (longest live name is 101 chars); CSS does the visual truncating.
const NAME_MAX = 128;
const TIERS = ["settled", "estimated", "provisional", "none"] as const;
// the tier mart is deploy-order optional — a row without it serves "unknown", which is a real badge
const tierOf = (v: unknown): Tier => ((TIERS as readonly string[]).includes(String(v)) ? (String(v) as Tier) : "unknown");
const countsOver = (keys: readonly string[]) => (t: unknown): Counts => {
  const src = obj(t);
  const o: Counts = {};
  for (const k of keys) {
    const n = num(src[k]);
    if (n) o[k] = n;
  }
  return o;
};
const tierMix = countsOver(TIERS);
// the aggregate marts can serve an "unknown" bucket the four-tier row mix drops — coverage and the
// overview stack their series over this one spelling
export const TIER_KEYS: readonly string[] = [...TIERS, "unknown"];
const tierCounts = countsOver(TIER_KEYS);
// a class the frontend has no chip for would render as an unlabelled doorway — drop it
const classCounts = (c: unknown): Counts => {
  const src = obj(c);
  const o: Counts = {};
  for (const k of FLAG_CLASSES) {
    const n = num(src[k]);
    if (n != null) o[k] = n;
  }
  return o;
};
const pair = (e: unknown): unknown[] => (Array.isArray(e) ? e : []);
// a malformed pair is dropped, never laundered: the coerced value is null-checked before it is typed
const dayPairs = (a: unknown): DayCount[] =>
  (Array.isArray(a) ? a : []).flatMap((e): DayCount[] => {
    const d = text(pair(e)[0], 10);
    const n = num(pair(e)[1]);
    return d === "" || n === null ? [] : [[d, n]];
  });
const tierDays = (a: unknown): DayCounts[] =>
  (Array.isArray(a) ? a : [])
    .map((e): DayCounts => [text(pair(e)[0], 10), tierCounts(pair(e)[1])])
    .filter(([d]) => d !== "");
const estDays = (a: unknown): EstDay[] =>
  (Array.isArray(a) ? a : [])
    .map((e): EstDay => [text(pair(e)[0], 10), num(pair(e)[1]), num(pair(e)[2]) ?? 0])
    .filter(([d]) => d !== "");
const airport = (e: unknown): Airport => {
  const src = obj(e);
  return { icao: text(src.icao, 8), iata: text(src.iata, 4), city: text(src.city, 48) };
};
const apCode = (e: Airport) => e.iata || e.icao || ""; // IATA-or-ICAO, the /flights coalesce
const odChip = (r: Raw): OdChip => ({ o: text(r.o, 8), d: text(r.d, 8), n: num(r.n) ?? 0 });
const page = <T>(j: Raw, rows: T[]): Paged<T> => ({
  rows,
  total: num(j.total) ?? rows.length,
  limit: num(j.limit) ?? rows.length,
  offset: num(j.offset) ?? 0,
});

function instance(r: Raw): Instance {
  const hex = text(r.icao24, 6).toLowerCase();
  const startTs = num(r.start_ts);
  const cs = csKeyOf(r.callsign);
  const origin = airport(r.origin);
  const dest = airport(r.dest);
  return {
    hex,
    day: text(r.day, 10),
    startTs,
    endTs: num(r.end_ts),
    callsign: text(r.callsign, 16),
    airline: text(r.airline, NAME_MAX),
    reg: text(r.registration, 12),
    type: text(r.typecode, 8),
    origin,
    dest,
    o: apCode(origin),
    d: apCode(dest),
    tier: tierOf(r.tier),
    gapS: num(r.effective_gap_s),
    nPoints: num(r.n_points),
    mil: r.is_military === true || num(r.is_military) === 1,
    // ephemeral click key only — the path fetch happens now, nothing durable is stored on it
    flightId: r.flight_id == null ? null : text(r.flight_id, 24),
    // callsign segment disambiguates real same-hex-same-second collisions (22 live keys, 21 on
    // distinct routes) — hex+epoch alone aliases two flights there
    key: hex && startTs != null ? `${hex}.${Math.round(startTs)}${cs ? `.${cs}` : ""}` : null,
  };
}

export async function fetchAirlines(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Paged<AirlineRow> | null> {
  const j = await call("airlines", `/workbench/airlines${qs(params)}`, signal);
  if (!j) return null;
  return page(
    j,
    list(j.airlines).map((r) => ({
      name: text(r.name, NAME_MAX),
      nFlights: num(r.n_flights) ?? 0,
      nServices: num(r.n_services) ?? 0,
      firstDay: text(r.first_day, 10),
      lastDay: text(r.last_day, 10),
      tiers: tierMix(r.tiers),
    })),
  );
}

export async function fetchServices(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Paged<ServiceRow> | null> {
  const j = await call("services", `/workbench/services${qs(params)}`, signal);
  if (!j) return null;
  return page(
    j,
    list(j.services).map((r) => ({
      callsign: text(r.callsign, 16),
      nInstances: num(r.n_instances) ?? 0,
      topOd: list(r.top_od).map(odChip),
      firstDay: text(r.first_day, 10),
      lastDay: text(r.last_day, 10),
      tiers: tierMix(r.tiers),
    })),
  );
}

export async function fetchInstances(
  params: Record<string, unknown>, lane: Lane = "instances", signal?: AbortSignal,
): Promise<InstancePage | null> {
  const j = await call(lane, `/workbench/instances${qs(params)}`, signal);
  if (!j) return null;
  return {
    ...page(j, list(j.instances).map(instance)),
    od: list(j.od_breakdown).map(odChip),
    // absent-not-hidden: only an explicit false (tier mart missing) disables the military control
    milAvailable: j.military_filter_available !== false,
  };
}

export async function fetchSummary(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Summary | null> {
  const j = await call("summary", `/workbench/summary${qs(params)}`, signal);
  if (!j) return null;
  if (j.complete === false) return null; // server-marked outage renders as unavailable, never as zeros
  const f = obj(j.flags);
  const t = obj(j.tiers);
  const e = obj(j.est);
  return {
    flights: num(j.flights) ?? 0,
    aircraft: num(j.aircraft) ?? 0,
    services: num(j.services) ?? 0,
    daily: dayPairs(j.daily),
    // absent-not-hidden again: only an explicit false (mart missing) ghosts the section
    flags: { available: f.available !== false, flagged: num(f.flagged) ?? 0, classes: classCounts(f.classes) },
    tiers: { available: t.available !== false, mix: tierCounts(t.mix), daily: tierDays(t.daily) },
    est: { available: e.available !== false, errP50Km: num(e.err_p50_km), n: num(e.n) ?? 0, daily: estDays(e.daily) },
    movers: list(j.movers).map((r) => ({
      key: text(r.key, NAME_MAX),
      n: num(r.n) ?? 0,
      prevN: num(r.prev_n) ?? 0,
      deltaPct: num(r.delta_pct),
    })),
  };
}

export async function fetchTrends(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Trends | null> {
  const j = await call("trends", `/workbench/trends${qs(params)}`, signal);
  if (!j) return null;
  if (j.complete === false) return null; // server-marked outage renders as unavailable, never as zeros
  // an airline name is the key here, so the key cap is the filter-value cap, not a label width
  const rank = list(j.rank).map((r) => ({
    key: text(r.key, NAME_MAX),
    n: num(r.n) ?? 0,
    distinctAircraft: num(r.distinct_aircraft) ?? 0,
    prevN: num(r.prev_n) ?? 0,
    deltaPct: num(r.delta_pct),
  }));
  const { total, limit, offset } = page(j, rank);
  return {
    dim: text(j.dim, 16),
    series: list(j.series).map((s) => ({ key: text(s.key, NAME_MAX), points: dayPairs(s.points) })),
    rank,
    total,
    limit,
    offset,
  };
}

export async function fetchFlags(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<FlagsPage | null> {
  const j = await call("flags", `/workbench/flags${qs(params)}`, signal);
  if (!j) return null;
  if (j.complete === false) return null; // server-marked outage renders as unavailable, never as zeros
  return {
    available: j.available !== false,
    ...page(
      j,
      list(j.flags).map((r) => ({
        ...instance(r),
        flagClass: text(r.flag_class, 24),
        detail: text(r.detail, 96),
      })),
    ),
    classes: classCounts(j.classes),
  };
}

// config_hash is a UInt64 the server already stringified — it stays an opaque key, never a Number.
const cfgKey = (v: unknown) => text(v, 24);
const MIX_DIMS = ["skip", "segment_kind", "uncertainty_bin"];
const mixRows = (a: unknown): MixRow[] =>
  list(a)
    .map((r) => ({ value: text(r.value, 48), producer: text(r.producer, 24), n: num(r.n) ?? 0 }))
    .filter((r) => r.value !== "");

export async function fetchEstimates(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Estimates | null> {
  const j = await call("estimates", `/workbench/estimates${qs(params)}`, signal);
  if (!j) return null;
  if (j.complete === false) return null; // server-marked outage renders as unavailable, never as zeros
  const m = obj(j.mix);
  const o = obj(j.outcomes);
  const i = obj(j.input_split);
  const mix: Record<string, MixRow[]> = {};
  // a dimension the view has no panel for is dropped, the classCounts precedent
  for (const d of MIX_DIMS) mix[d] = mixRows(m[d]);
  return {
    available: j.available !== false,
    headline: list(j.headline).map((r) => ({
      configHash: cfgKey(r.config_hash),
      n: num(r.n) ?? 0,
      p50Km: num(r.p50_km),
      p90Km: num(r.p90_km),
      firstDay: text(r.first_day, 10),
      lastDay: text(r.last_day, 10),
    })),
    daily: list(j.daily)
      .map((r) => ({
        day: text(r.day, 10),
        configHash: cfgKey(r.config_hash),
        p50Km: num(r.p50_km),
        p90Km: num(r.p90_km),
        n: num(r.n) ?? 0,
      }))
      .filter((r) => r.day !== ""),
    mix: { available: m.available !== false, ...mix },
    outcomes: {
      settled: num(o.settled) ?? 0,
      awaiting: num(o.awaiting) ?? 0,
      ambiguous: num(o.ambiguous) ?? 0,
    },
    inputSplit: { provisional: num(i.provisional) ?? 0, settled: num(i.settled) ?? 0 },
  };
}

export async function fetchCoverage(
  params: Record<string, unknown>, signal?: AbortSignal,
): Promise<Coverage | null> {
  const j = await call("coverage", `/workbench/coverage${qs(params)}`, signal);
  if (!j) return null;
  if (j.complete === false) return null; // server-marked outage renders as unavailable, never as zeros
  return {
    available: j.available !== false,
    tierDaily: tierDays(j.tier_daily),
    // ge is the bin's identity — a bin without one can't be placed on the axis
    gapBins: list(j.gap_bins).flatMap((b): GapBin[] => {
      const ge = num(b.ge);
      return ge === null ? [] : [{ ge, lt: num(b.lt), n: num(b.n) ?? 0 }];
    }),
    observed: list(j.observed).flatMap((r): Observed[] => {
      const day = text(r.day, 10);
      const median = num(r.median);
      return day === "" || median === null ? [] : [{ day, median, n: num(r.n) ?? 0 }];
    }),
  };
}

export async function fetchSearch(q: string, limit = 8): Promise<SearchResult | null> {
  const j = await call("search", `/workbench/search${qs({ q, limit })}`);
  if (!j) return null;
  return {
    airlines: list(j.airlines).map((r) => ({ name: text(r.name, NAME_MAX), n: num(r.n_flights) ?? 0 })),
    services: list(j.services).map((r) => ({
      callsign: text(r.callsign, 16),
      airline: text(r.airline, NAME_MAX),
      n: num(r.n_instances) ?? 0,
    })),
    airframes: list(j.airframes).map((r) => ({
      hex: text(r.icao24, 6).toLowerCase(),
      reg: text(r.registration, 12),
      type: text(r.typecode, 8),
      n: num(r.n_instances) ?? 0,
    })),
    airports: list(j.airports).map((r) => ({
      icao: text(r.icao, 8),
      iata: text(r.iata, 4),
      name: text(r.name, 64),
      city: text(r.city, 48),
    })),
  };
}
