from typing import Literal

from pydantic import BaseModel, ConfigDict

# OpenAPI + test-time shape of every workbench envelope; the hot path never validates through these
# (routes return JSONResponse directly), so a shape drift fails the snapshot/builder tests, not a request.
Tier = Literal["settled", "estimated", "provisional", "none", "unknown"]
Counts = dict[str, int]  # tier / flag-class buckets, aggregate-side vocabulary
DayCount = tuple[str, int]
DayCounts = tuple[str, Counts]
DayErr = tuple[str, float, int]  # day, err_p50_km, n


class WbModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paged(WbModel):
    total: int
    limit: int
    offset: int


class Airport(WbModel):
    icao: str | None
    iata: str | None
    city: str | None


class OdChip(WbModel):
    o: str
    d: str
    n: int


class AirlineRow(WbModel):
    name: str
    n_flights: int
    n_services: int
    first_day: str
    last_day: str
    tiers: Counts


class Airlines(Paged):
    airlines: list[AirlineRow]


class ServiceRow(WbModel):
    callsign: str
    n_instances: int
    top_od: list[OdChip]
    first_day: str
    last_day: str
    tiers: Counts


class Services(Paged):
    services: list[ServiceRow]


class InstanceRow(WbModel):
    flight_id: str | None
    day: str
    start_ts: float | None
    end_ts: float | None
    icao24: str | None
    registration: str | None
    typecode: str | None
    callsign: str | None
    airline: str | None
    origin: Airport
    dest: Airport
    tier: Tier
    effective_gap_s: float | None
    n_points: int | None
    is_military: bool


class Instances(Paged):
    instances: list[InstanceRow]
    od_breakdown: list[OdChip]
    military_filter_available: bool = True


class SummaryFlags(WbModel):
    available: bool
    flagged: int
    classes: Counts


class SummaryTiers(WbModel):
    available: bool
    mix: Counts
    daily: list[DayCounts]


class SummaryEst(WbModel):
    available: bool
    err_p50_km: float | None
    n: int
    daily: list[DayErr]


class Mover(WbModel):
    key: str
    n: int
    prev_n: int
    delta_pct: float | None


class Summary(WbModel):
    flights: int
    aircraft: int
    services: int
    daily: list[DayCount]
    flags: SummaryFlags
    tiers: SummaryTiers
    est: SummaryEst
    movers: list[Mover]
    complete: bool = True


class RankRow(WbModel):
    key: str
    n: int
    distinct_aircraft: int
    prev_n: int
    delta_pct: float | None


class Series(WbModel):
    key: str
    points: list[DayCount]


class Trends(Paged):
    dim: Literal["route", "airline", "airport"]
    grain: Literal["day"]
    series: list[Series]
    rank: list[RankRow]
    complete: bool = True


class FlagRow(InstanceRow):
    flag_class: str
    detail: str | None


class Flags(Paged):
    available: bool
    flags: list[FlagRow]
    classes: Counts
    complete: bool = True


class EstHeadline(WbModel):
    config_hash: str | None
    n: int
    p50_km: float | None
    p90_km: float | None
    first_day: str
    last_day: str


class EstDaily(WbModel):
    day: str
    config_hash: str | None
    p50_km: float | None
    p90_km: float | None
    n: int


class MixRow(WbModel):
    value: str
    producer: str
    n: int


class EstMix(WbModel):
    available: bool
    skip: list[MixRow]
    segment_kind: list[MixRow]
    uncertainty_bin: list[MixRow]


class EstOutcomes(WbModel):
    settled: int
    awaiting: int
    ambiguous: int


class EstInputSplit(WbModel):
    provisional: int
    settled: int


class Estimates(WbModel):
    available: bool
    headline: list[EstHeadline]
    daily: list[EstDaily]
    mix: EstMix
    outcomes: EstOutcomes
    input_split: EstInputSplit
    complete: bool = True


class GapBin(WbModel):
    ge: int
    lt: int | None
    n: int


class Observed(WbModel):
    day: str
    median: float
    n: int


class Coverage(WbModel):
    available: bool
    tier_daily: list[DayCounts]
    gap_bins: list[GapBin]
    observed: list[Observed]
    complete: bool = True


class SearchAirline(WbModel):
    name: str
    n_flights: int


class SearchService(WbModel):
    callsign: str
    airline: str | None
    n_instances: int


class SearchAirframe(WbModel):
    icao24: str
    registration: str | None
    typecode: str | None
    n_instances: int


class SearchAirport(WbModel):
    icao: str
    iata: str | None
    name: str | None
    city: str | None


class Search(WbModel):
    airlines: list[SearchAirline]
    services: list[SearchService]
    airframes: list[SearchAirframe]
    airports: list[SearchAirport]


class FeatureFlags(WbModel):
    workbench: bool


class Features(WbModel):
    features: FeatureFlags
    contract: int


# route path -> envelope; the OpenAPI, snapshot and wire.d.ts all iterate this one table
ENVELOPES = {
    "/features": Features,
    "/workbench/airlines": Airlines,
    "/workbench/services": Services,
    "/workbench/instances": Instances,
    "/workbench/summary": Summary,
    "/workbench/trends": Trends,
    "/workbench/flags": Flags,
    "/workbench/estimates": Estimates,
    "/workbench/coverage": Coverage,
    "/workbench/search": Search,
}
