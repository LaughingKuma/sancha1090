// Hand-written mirror of livemap/wb_models.py (names match); the schema snapshot pins the Python side and
// this file is the TypeScript reader's picture of the same ten envelopes.
export type Tier = "settled" | "estimated" | "provisional" | "none" | "unknown";
export type Counts = Record<string, number>; // tier / flag-class buckets
export type DayCount = [string, number];
export type DayCounts = [string, Counts];
export type DayErr = [string, number, number]; // day, err_p50_km, n
export interface Paged { total: number; limit: number; offset: number }

export interface Airport { icao: string | null; iata: string | null; city: string | null }
export interface OdChip { o: string; d: string; n: number }

export interface AirlineRow {
  name: string; n_flights: number; n_services: number; first_day: string; last_day: string; tiers: Counts;
}
export interface Airlines extends Paged { airlines: AirlineRow[] }

export interface ServiceRow {
  callsign: string; n_instances: number; top_od: OdChip[]; first_day: string; last_day: string; tiers: Counts;
}
export interface Services extends Paged { services: ServiceRow[] }

export interface InstanceRow {
  flight_id: string | null; day: string; start_ts: number | null; end_ts: number | null;
  icao24: string | null; registration: string | null; typecode: string | null; callsign: string | null;
  airline: string | null; origin: Airport; dest: Airport; tier: Tier;
  effective_gap_s: number | null; n_points: number | null; is_military: boolean;
}
export interface Instances extends Paged {
  instances: InstanceRow[]; od_breakdown: OdChip[];
  military_filter_available?: boolean;
}

export interface SummaryFlags { available: boolean; flagged: number; classes: Counts }
export interface SummaryTiers { available: boolean; mix: Counts; daily: DayCounts[] }
export interface SummaryEst { available: boolean; err_p50_km: number | null; n: number; daily: DayErr[] }
export interface Mover { key: string; n: number; prev_n: number; delta_pct: number | null }
export interface Summary {
  flights: number; aircraft: number; services: number; daily: DayCount[];
  flags: SummaryFlags; tiers: SummaryTiers; est: SummaryEst; movers: Mover[]; complete?: boolean;
}

export interface RankRow { key: string; n: number; distinct_aircraft: number; prev_n: number; delta_pct: number | null }
export interface Series { key: string; points: DayCount[] }
export interface Trends extends Paged {
  dim: "route" | "airline" | "airport"; grain: "day"; series: Series[]; rank: RankRow[]; complete?: boolean;
}

export interface FlagRow extends InstanceRow { flag_class: string; detail: string | null }
export interface Flags extends Paged {
  available: boolean; flags: FlagRow[]; classes: Counts; complete?: boolean;
}

export interface EstHeadline {
  config_hash: string | null; n: number; p50_km: number | null; p90_km: number | null; first_day: string; last_day: string;
}
export interface EstDaily { day: string; config_hash: string | null; p50_km: number | null; p90_km: number | null; n: number }
export interface MixRow { value: string; producer: string; n: number }
export interface EstMix { available: boolean; skip: MixRow[]; segment_kind: MixRow[]; uncertainty_bin: MixRow[] }
export interface EstOutcomes { settled: number; awaiting: number; ambiguous: number }
export interface EstInputSplit { provisional: number; settled: number }
export interface Estimates {
  available: boolean; headline: EstHeadline[]; daily: EstDaily[]; mix: EstMix;
  outcomes: EstOutcomes; input_split: EstInputSplit; complete?: boolean;
}

export interface GapBin { ge: number; lt: number | null; n: number }
export interface Observed { day: string; median: number; n: number }
export interface Coverage {
  available: boolean; tier_daily: DayCounts[]; gap_bins: GapBin[]; observed: Observed[]; complete?: boolean;
}

export interface SearchAirline { name: string; n_flights: number }
export interface SearchService { callsign: string; airline: string | null; n_instances: number }
export interface SearchAirframe { icao24: string; registration: string | null; typecode: string | null; n_instances: number }
export interface SearchAirport { icao: string; iata: string | null; name: string | null; city: string | null }
export interface Search {
  airlines: SearchAirline[]; services: SearchService[]; airframes: SearchAirframe[]; airports: SearchAirport[];
}

export interface FeatureFlags { workbench: boolean }
export interface Features { features: FeatureFlags; contract: number }

// baked by vite.workbench.config.js `define`; the entry compares it to Features.contract before mounting
declare global { const __WB_CONTRACT__: number }
