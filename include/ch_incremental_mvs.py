from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Cheap aggregates as self-maintaining AggregatingMergeTree MVs. Three invariants the code depends on:
# each spec's "read" is the merge-aware read contract; raw *_state columns are opaque (uniqExactMerge/sum + GROUP BY only).
# MVs attach to append-only bronze, never the dbt-REPLACE'd silver (an MV can't survive a drop+recreate).
# Context-lane obs = uniqExact((icao24,snapshot_time)): dedup-immune (an MV can't dedup across blocks),
# where ADS-B is row-preserving over bronze so plain count() suffices.

_OPENSKY = "bronze.opensky_states"
_ADSBLOL = "bronze.adsblol_states"
_ADSB = "bronze.adsb_states"
_DIM_AIRLINES = "silver_ch.dim_airlines"

# Japan+ocean box (v5.0) — mirrors include/regions.py / stg_states.sql, kept in sync by hand.
_GEO = "latitude BETWEEN 20 AND 50 AND longitude BETWEEN 122 AND 165"
# Operating airline via callsign 3-letter prefix; same GA-tail regex guard as the dbt models.
_AL_JOIN = ("JOIN {dim} al ON al.icao = substring(trimBoth({cs}), 1, 3) "
            "AND match(trimBoth({cs}), '^[A-Z]{{3}}[0-9]')")
# Callsign-backfill window (var('callsign_backfill_window_s')); kept in sync with dbt_project.yml.
_BF_WINDOW_S = 600

# hex -> reg_country via the P1 range_hashed dict, mirroring macros/ch_compat.ch_hex_country.
_HEX_COUNTRY = (
    "if(match(lower(coalesce(hex, '')), '^[0-9a-f]{1,6}$'), "
    "dictGetOrNull('dim.dict_hex_country', 'country', toUInt8(0), "
    "reinterpretAsUInt32(reverse(unhex(leftPad(lower(coalesce(hex, '')), 8, '0'))))), NULL)"
)
# dbFlags bit 0 = military; the baked db_flags column (v6.3) replaces JSONExtractInt(_raw_json,...) (absent=0).
_IS_MILITARY = "bitAnd(db_flags, 1) != 0"

# ADS-B airline callsign-backfill: SEED and MV need DIFFERENT engines for the SAME two-sided nearest-pick
# because two ASOF joins in one CH MaterializedView are broken (26.5.1: the 2nd ASOF's value columns don't
# materialize) — seed sort-merges two ASOF + row_number over full bronze, MV equi-joins per block + argMinIf
# (a regular join would explode 20M adsb x OpenSky-per-hex over full bronze; the per-block one is ~0.2 s).
# argMin's tuple key == dbt's row_number tie-break and is deterministic where ASOF isn't (dup snapshots).
_OPENSKY_CALLSIGN = (
    f"SELECT icao24, toUnixTimestamp64Micro(snapshot_time) / 1e6 AS snap_epoch, trimBoth(callsign) AS callsign "
    f"FROM {_OPENSKY} WHERE callsign IS NOT NULL AND trimBoth(callsign) <> ''"
)
_BF_PREC_OK = f"(p.callsign IS NOT NULL AND (s.capture_ts - p.snap_epoch) <= {_BF_WINDOW_S})"
_BF_FOLL_OK = f"(f.callsign IS NOT NULL AND (f.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S})"
# SEED form: nearest of the two ASOF hits; tie -> following (== dbt snap_epoch DESC), so preceding wins on STRICT <.
_ADSB_AIRLINE_SEED_BODY = f"""
SELECT
    toStartOfHour(toDateTime(d.capture_ts))       AS snapshot_hour,
    al.name                                       AS airline_name,
    al.country                                    AS airline_country,
    uniqExactState(d.hex)                         AS distinct_aircraft_state,
    uniqExactState((d.hex, d.capture_ts))         AS observations,
    uniqExactStateIf((d.hex, d.capture_ts), d.callsign_source = 'opensky_backfill') AS backfilled_observations
FROM (
    SELECT
        s.hex AS hex,
        s.capture_ts AS capture_ts,
        coalesce(nullIf(trimBoth(s.flight), ''),
            multiIf(NOT {_BF_PREC_OK} AND NOT {_BF_FOLL_OK}, NULL,
                    NOT {_BF_FOLL_OK}, p.callsign,
                    NOT {_BF_PREC_OK}, f.callsign,
                    (s.capture_ts - p.snap_epoch) < (f.snap_epoch - s.capture_ts), p.callsign,
                    f.callsign))                  AS callsign_filled,
        multiIf(nullIf(trimBoth(s.flight), '') IS NOT NULL, 'adsb',
                ({_BF_PREC_OK} OR {_BF_FOLL_OK}), 'opensky_backfill', NULL) AS callsign_source
    FROM {_ADSB} s
    ASOF LEFT JOIN ({_OPENSKY_CALLSIGN}) p ON p.icao24 = s.hex AND p.snap_epoch <= s.capture_ts
    ASOF LEFT JOIN ({_OPENSKY_CALLSIGN}) f ON f.icao24 = s.hex AND f.snap_epoch >= s.capture_ts
) d
{_AL_JOIN.format(dim=_DIM_AIRLINES, cs="d.callsign_filled")}
GROUP BY snapshot_hour, airline_name, airline_country
""".strip()
# MV form: regular equi-join (block is tiny) + argMinIf nearest by tuple (abs_dist, -snap_epoch, callsign)
# == dbt's ORDER BY abs(d) ASC, snap_epoch DESC, callsign ASC. observations is uniqExact((hex,capture_ts)) (not
# count()) so a cross-block crash-replay dup can't inflate it (see the count()-not-dup-immune note below).
def _adsb_attribution(opensky_src: str, where_sql: str = "", alias_pad: str = " " * 9) -> str:
    # (hex,capture_ts)->callsign attribution shared by the MV body and the windowed oracle; alias_pad
    # keeps each caller's historical alignment (the oracle feeds the served-value gate — zero byte drift).
    return f"""    SELECT
        s.hex AS hex,
        s.capture_ts AS capture_ts,
        coalesce(nullIf(trimBoth(any(s.flight)), ''),
                 argMinIf(o.callsign, (abs(o.snap_epoch - s.capture_ts), -o.snap_epoch, o.callsign),
                          o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S})) AS callsign_filled,
        multiIf(nullIf(trimBoth(any(s.flight)), '') IS NOT NULL, 'adsb',
                countIf(o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S}) > 0,
                'opensky_backfill', NULL){alias_pad}AS callsign_source
    FROM {_ADSB} s
    LEFT JOIN ({opensky_src}) o ON o.icao24 = s.hex{where_sql}
    GROUP BY s.hex, s.capture_ts"""


_ADSB_AIRLINE_MV_BODY = f"""
SELECT
    toStartOfHour(toDateTime(t.capture_ts))       AS snapshot_hour,
    al.name                                       AS airline_name,
    al.country                                    AS airline_country,
    uniqExactState(t.hex)                         AS distinct_aircraft_state,
    uniqExactState((t.hex, t.capture_ts))         AS observations,
    uniqExactStateIf((t.hex, t.capture_ts), t.callsign_source = 'opensky_backfill') AS backfilled_observations
FROM (
{_adsb_attribution(_OPENSKY_CALLSIGN)}
) t
{_AL_JOIN.format(dim=_DIM_AIRLINES, cs="t.callsign_filled")}
GROUP BY snapshot_hour, airline_name, airline_country
""".strip()

# OpenSky-context per-hour measures, all dedup-immune across MV blocks (an MV can't dedup (icao24,
# snapshot_time)). Counts use uniqExact. avg_speed needs a DEDUPED velocity sum: a raw sumIf/countIf
# is dup-WEIGHTED and wrong when dup multiplicity varies across the hour (verified: 11 hours off, max
# 5.64 km/h). maxMap keyed by (icao24|snapshot_time) keeps one velocity per key (dups are velocity-
# identical, verified) so its merged values sum to the deduped numerator; the denominator is the deduped
# airborne+velocity count. Map keys/values must be non-Nullable.
_SPEED_KEY = "concat(assumeNotNull(icao24), '|', toString(assumeNotNull(snapshot_time)))"
_HOURLY_STATE_SELECT = f"""
    toStartOfHour(snapshot_time)                              AS snapshot_hour,
    uniqExactState(icao24)                                    AS unique_aircraft_state,
    uniqExactState((icao24, snapshot_time))                  AS total_obs_state,
    uniqExactStateIf((icao24, snapshot_time), NOT on_ground) AS airborne_obs_state,
    uniqExactStateIf((icao24, snapshot_time), on_ground)     AS on_ground_obs_state,
    maxMap(if(NOT on_ground AND velocity IS NOT NULL, map({_SPEED_KEY}, assumeNotNull(velocity) * 3.6), map())) AS airborne_speed_map,
    uniqExactStateIf((icao24, snapshot_time), NOT on_ground AND velocity IS NOT NULL) AS airborne_speed_cnt_state
""".strip()


def adsb_airline_oracle_sql(lo: int, hi: int) -> str:
    # Windowed PLAIN-count airline attribution for the served-value gate — the SAME (hex,capture_ts)->airline
    # mapping the MV maintains (two-sided argMinIf nearest; == the seed ASOF, verified diff-0), as exact counts per
    # (airline, hour) so the gate validates distinct_aircraft + observations + backfilled_observations exactly (not
    # just the native subset). opensky is bounded to [lo-window, hi+window] (the backfill only looks within
    # _BF_WINDOW_S of each capture_ts, so the window is result-preserving) to keep the join cost bounded.
    opensky_win = (f"{_OPENSKY_CALLSIGN} AND toUnixTimestamp64Micro(snapshot_time) / 1e6 "
                   f"BETWEEN {lo - _BF_WINDOW_S} AND {hi + _BF_WINDOW_S}")
    where_sql = f"\n    WHERE s.capture_ts >= {lo} AND s.capture_ts < {hi}"
    return f"""
SELECT
    coalesce(al.name, '')                                    AS airline_name,
    coalesce(al.country, '')                                 AS airline_country,
    toUnixTimestamp(toStartOfHour(toDateTime(t.capture_ts))) AS h,
    uniqExact(t.hex)                                         AS distinct_aircraft,
    uniqExact((t.hex, t.capture_ts))                        AS observations,
    uniqExactIf((t.hex, t.capture_ts), t.callsign_source = 'opensky_backfill') AS backfilled
FROM (
{_adsb_attribution(opensky_win, where_sql, alias_pad=" " * 20)}
) t
{_AL_JOIN.format(dim=_DIM_AIRLINES, cs="t.callsign_filled")}
GROUP BY airline_name, airline_country, h
""".strip()


# SWIM latest-amendment identity — the SINGLE source of this expression (dbt's int_swim_latest reads the
# -Merge serving view instead of re-deriving it; tests/test_swim_latest_sql.py pins that there's no copy).
_SWIM_FLIGHT_KEY = "coalesce(gufi, flight_ref, concat(assumeNotNull(acid), '|', ifNull(computer_id,''), '|', toString(toDate(filed_departure_time))))"
# argMax over (msg_timestamp, _dedup_fp) is idempotent under duplicate re-insertion, so the MV needs no dedup
# (same safety class as the uniqExact accs); the 7-column value tuple order IS a contract — dbt reads .1..7.
_SWIM_LATEST_SELECT = f"""
SELECT
    {_SWIM_FLIGHT_KEY} AS flight_key,
    argMaxState(tuple(dep_point, dep_point_kind, arr_point, arr_point_kind,
                      filed_departure_time, filed_arrival_time, acid),
                tuple(msg_timestamp, _dedup_fp)) AS latest_state
FROM bronze.swim_flightdata
WHERE acid IS NOT NULL AND trimBoth(acid) <> ''
GROUP BY flight_key
""".strip()


def _spec():
    # Each spec: target DDL + MV DDL + one-time seed INSERTs + the merge-aware read (parity / P5 view).
    # Optional keys: "db" (default gold_ch) and "view" (serving-view base name, default drop_old[0]).
    obs_tuple = "AggregateFunction(uniqExact, Tuple(Nullable(String), Nullable(DateTime64(6, 'UTC'))))"
    uniq_str = "AggregateFunction(uniqExact, Nullable(String))"
    # ADS-B obs grain is now (group, hour) (v6.3 re-grain) so uniqExact is affordable AND exact: each
    # (hex, capture_ts) lands in one disjoint hour, so uniqExactMerge over a group's hours == the group total;
    # replay-immune (merge unions states) and bounded by a 90d TTL. capture_ts is Float64 epoch.
    adsb_obs = "AggregateFunction(uniqExact, Tuple(Nullable(String), Nullable(Float64)))"

    specs = {}

    # acc parts are the only non-rederivable state (reseed is an operator action); fsync closes the
    # crash-loss class from #116 — parts are tiny+hourly, cost is negligible.

    # 1) Hourly traffic — accumulate-forever (replaces agg_hourly_traffic{,_adsblol,_opensky_settled}).
    specs["agg_hourly_traffic_acc"] = {
        "description": "gold_ch.agg_hourly_traffic: one row per UTC hour over the OpenSky context feed inside "
                       "the Japan box, accumulated forever; hours before the OpenSky lane's first hour are "
                       "seeded once from bronze.adsblol_states (region = 'japan'), so the pre-pipeline history "
                       "is adsb.lol's. Observation counts are uniqExact over (icao24, snapshot_time) tuples, "
                       "not count(), so a replayed bronze file cannot inflate them; avg_airborne_speed_kmh is "
                       "a deduped mean: one velocity per (icao24, snapshot_time) held in a maxMap, summed, "
                       "then divided by the deduped count.",
        "drop_old": ["agg_hourly_traffic", "agg_hourly_traffic_history", "agg_hourly_traffic_live_archive",
                     "agg_hourly_traffic_adsblol", "agg_hourly_traffic_opensky_settled"],
        "target": f"""
CREATE TABLE IF NOT EXISTS gold_ch.agg_hourly_traffic_acc
(
    snapshot_hour            DateTime,
    unique_aircraft_state    {uniq_str},
    total_obs_state          {obs_tuple},
    airborne_obs_state       {obs_tuple},
    on_ground_obs_state      {obs_tuple},
    airborne_speed_map       SimpleAggregateFunction(maxMap, Map(String, Float64)),
    airborne_speed_cnt_state {obs_tuple}
)
ENGINE = AggregatingMergeTree
ORDER BY snapshot_hour
SETTINGS fsync_after_insert = 1, fsync_part_directory = 1
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS gold_ch.agg_hourly_traffic_acc_mv
TO gold_ch.agg_hourly_traffic_acc AS
SELECT
{_HOURLY_STATE_SELECT}
FROM {_OPENSKY}
WHERE {_GEO} AND latitude IS NOT NULL AND longitude IS NOT NULL
GROUP BY snapshot_hour
""".strip(),
        # Live = all bronze history; adsblol = adsblol hours strictly below the live floor (disjoint, no
        # double-count).
        "seed": [
            f"""
INSERT INTO gold_ch.agg_hourly_traffic_acc
SELECT
{_HOURLY_STATE_SELECT}
FROM {_OPENSKY}
WHERE {_GEO} AND latitude IS NOT NULL AND longitude IS NOT NULL
GROUP BY snapshot_hour
""".strip(),
            f"""
INSERT INTO gold_ch.agg_hourly_traffic_acc
SELECT
{_HOURLY_STATE_SELECT}
FROM {_ADSBLOL}
WHERE region = 'japan' AND latitude IS NOT NULL AND longitude IS NOT NULL
GROUP BY snapshot_hour
-- coalesce: an empty live lane (blank-warehouse bootstrap) has a NULL floor; seed ALL adsblol hours then.
HAVING snapshot_hour < coalesce(
    (SELECT min(toStartOfHour(snapshot_time)) FROM {_OPENSKY} WHERE {_GEO}),
    toDateTime('2099-01-01 00:00:00', 'UTC'))
""".strip(),
        ],
        "read": """
SELECT
    snapshot_hour,
    uniqExactMerge(unique_aircraft_state)  AS unique_aircraft,
    uniqExactMerge(total_obs_state)        AS total_observations,
    uniqExactMerge(airborne_obs_state)     AS airborne_observations,
    uniqExactMerge(on_ground_obs_state)    AS on_ground_observations,
    -- deduped avg: maxMap-merged values (one velocity per (icao24,snapshot_time)) over the deduped count.
    round(arraySum(mapValues(maxMap(airborne_speed_map))) / nullIf(uniqExactMerge(airborne_speed_cnt_state), 0), 2) AS avg_airborne_speed_kmh
FROM gold_ch.agg_hourly_traffic_acc
GROUP BY snapshot_hour
ORDER BY snapshot_hour
""".strip(),
    }

    # 2) Airline traffic (OpenSky context) — hourly grain.
    # Accumulate-forever read since the 2026-07 stg_states unwindowing (the old 30d parity window inverted).
    al_join = _AL_JOIN.format(dim=_DIM_AIRLINES, cs="s.callsign")
    # One SELECT shared by mv + seed (the country_select precedent): the two must aggregate identically.
    airline_select = f"""
SELECT
    toStartOfHour(s.snapshot_time)                AS snapshot_hour,
    al.name                                       AS airline_name,
    al.country                                    AS airline_country,
    uniqExactState(s.icao24)                      AS distinct_aircraft_state,
    uniqExactState((s.icao24, s.snapshot_time))   AS observations_state
FROM {_OPENSKY} s
{al_join}
WHERE {_GEO}
GROUP BY snapshot_hour, airline_name, airline_country
""".strip()
    specs["agg_airline_traffic_acc"] = {
        "description": "gold_ch.agg_airline_traffic: one row per (UTC hour, airline) over the OpenSky context "
                       "feed, accumulated forever. The airline join is an INNER join through dim_airlines behind "
                       "the ^[A-Z]{3}[0-9] callsign guard, so unmatched and GA-shaped callsigns are excluded "
                       "outright — this mart counts airline traffic, never all traffic.",
        "drop_old": ["agg_airline_traffic"],
        "target": f"""
CREATE TABLE IF NOT EXISTS gold_ch.agg_airline_traffic_acc
(
    snapshot_hour            DateTime,
    airline_name             String,
    airline_country          String,
    distinct_aircraft_state  {uniq_str},
    observations_state       {obs_tuple}
)
ENGINE = AggregatingMergeTree
ORDER BY (snapshot_hour, airline_name, airline_country)
SETTINGS fsync_after_insert = 1, fsync_part_directory = 1
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS gold_ch.agg_airline_traffic_acc_mv
TO gold_ch.agg_airline_traffic_acc AS
{airline_select}
""".strip(),
        "seed": [f"""
INSERT INTO gold_ch.agg_airline_traffic_acc
{airline_select}
""".strip()],
        "read": """
SELECT
    snapshot_hour, airline_name, airline_country,
    uniqExactMerge(distinct_aircraft_state) AS distinct_aircraft,
    uniqExactMerge(observations_state)      AS observations
FROM gold_ch.agg_airline_traffic_acc
GROUP BY snapshot_hour, airline_name, airline_country
ORDER BY snapshot_hour, distinct_aircraft DESC
""".strip(),
    }

    # 3) Airline traffic (rooftop ADS-B) — two-sided OpenSky callsign backfill (see _ADSB_AIRLINE_*_BODY).
    specs["agg_airline_traffic_adsb_acc"] = {
        "description": "gold_ch.agg_airline_traffic_adsb: one row per airline over the rooftop feed (stored "
                       "hourly; the read collapses hours over a rolling 90 days). Same INNER dim_airlines "
                       "join and callsign guard as the OpenSky sibling, so non-airline traffic is excluded. "
                       "backfilled_observations is the "
                       "subset of observations whose callsign came from the OpenSky backfill rather than the "
                       "rooftop transmission — subtract it for a rooftop-only count.",
        "drop_old": ["agg_airline_traffic_adsb"],
        "target": f"""
CREATE TABLE IF NOT EXISTS gold_ch.agg_airline_traffic_adsb_acc
(
    snapshot_hour            DateTime,
    airline_name             String,
    airline_country          String,
    distinct_aircraft_state  {uniq_str},
    observations             {adsb_obs},
    backfilled_observations  {adsb_obs}
)
ENGINE = AggregatingMergeTree
ORDER BY (snapshot_hour, airline_name, airline_country)
TTL snapshot_hour + INTERVAL 90 DAY
SETTINGS fsync_after_insert = 1, fsync_part_directory = 1
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS gold_ch.agg_airline_traffic_adsb_acc_mv
TO gold_ch.agg_airline_traffic_adsb_acc AS
{_ADSB_AIRLINE_MV_BODY}
""".strip(),
        "seed": [f"INSERT INTO gold_ch.agg_airline_traffic_adsb_acc\n{_ADSB_AIRLINE_SEED_BODY}"],
        # Read collapses hours (GROUP BY group only) so the served per-airline number/shape is unchanged; the
        # explicit 90d window makes the served number deterministic (the TTL drops lazily on merge), a rolling
        # window like the OpenSky sibling — the all-time HLL was unbounded.
        "read": """
SELECT
    airline_name, airline_country,
    uniqExactMerge(distinct_aircraft_state) AS distinct_aircraft,
    uniqExactMerge(observations)            AS observations,
    uniqExactMerge(backfilled_observations) AS backfilled_observations
FROM gold_ch.agg_airline_traffic_adsb_acc
WHERE snapshot_hour >= now('UTC') - INTERVAL 90 DAY
GROUP BY airline_name, airline_country
ORDER BY distinct_aircraft DESC
""".strip(),
    }

    # 4) Country traffic (rooftop ADS-B) — reg_country via the range_hashed dict, military via the baked db_flags.
    # observations/military use uniqExact((hex,capture_ts)) NOT count(): an MV can't dedup across blocks, so a
    # crash-replay would double-count under count(); uniqExact is replay-immune. v6.3 re-grain to (reg_country, hour).
    # The inner nullable country is aliased reg_country_n (NOT reg_country): if the outer alias shadowed it, CH
    # binds `WHERE reg_country IS NOT NULL` to the never-null assumeNotNull() alias — a no-op that leaks NULL-country
    # (untracked) hexes as '' and diverges from the value-gate oracle. Filter the nullable column, then assumeNotNull.
    country_select = f"""
    toStartOfHour(toDateTime(capture_ts))   AS snapshot_hour,
    assumeNotNull(reg_country_n)            AS reg_country,
    uniqExactState(hex)                     AS distinct_aircraft_state,
    uniqExactState((hex, capture_ts))       AS observations,
    uniqExactStateIf((hex, capture_ts), {_IS_MILITARY}) AS military_observations
FROM (SELECT {_HEX_COUNTRY} AS reg_country_n, hex, capture_ts, db_flags FROM {_ADSB})
WHERE reg_country_n IS NOT NULL
GROUP BY snapshot_hour, reg_country
""".strip()
    specs["agg_country_traffic_adsb_acc"] = {
        "description": "gold_ch.agg_country_traffic_adsb: one row per registration country over the rooftop "
                       "feed, served over a rolling 90 days (the read collapses the stored hourly grain). "
                       "Country comes from the hex range dictionary, so airframes with an unmapped hex are "
                       "dropped; military_observations is the db_flags bit-0 subset of observations.",
        "drop_old": ["agg_country_traffic_adsb"],
        "target": f"""
CREATE TABLE IF NOT EXISTS gold_ch.agg_country_traffic_adsb_acc
(
    snapshot_hour            DateTime,
    reg_country              String,
    distinct_aircraft_state  {uniq_str},
    observations             {adsb_obs},
    military_observations    {adsb_obs}
)
ENGINE = AggregatingMergeTree
ORDER BY (snapshot_hour, reg_country)
TTL snapshot_hour + INTERVAL 90 DAY
SETTINGS fsync_after_insert = 1, fsync_part_directory = 1
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS gold_ch.agg_country_traffic_adsb_acc_mv
TO gold_ch.agg_country_traffic_adsb_acc AS
SELECT
    {country_select}
""".strip(),
        "seed": [f"""
INSERT INTO gold_ch.agg_country_traffic_adsb_acc
SELECT
    {country_select}
""".strip()],
        # Read collapses hours (GROUP BY reg_country only) so the served per-country number/shape is unchanged; the
        # explicit 90d window makes the served number deterministic (the TTL drops lazily on merge).
        "read": """
SELECT
    reg_country,
    uniqExactMerge(distinct_aircraft_state) AS distinct_aircraft,
    uniqExactMerge(observations)            AS observations,
    uniqExactMerge(military_observations)   AS military_observations
FROM gold_ch.agg_country_traffic_adsb_acc
WHERE snapshot_hour >= now('UTC') - INTERVAL 90 DAY
GROUP BY reg_country
ORDER BY distinct_aircraft DESC
""".strip(),
    }

    # 5) SWIM latest amendment (#201) — supersedes a dbt full-scan, not a dbt table, so it has nothing to drop
    # and names its view directly; flight_key stays Nullable or '' would defeat _swim.yml's not_null tripwire.
    swim_state = ("AggregateFunction(argMax, Tuple(Nullable(String), Nullable(String), Nullable(String), "
                  "Nullable(String), Nullable(DateTime64(6, 'UTC')), Nullable(DateTime64(6, 'UTC')), "
                  "Nullable(String)), Tuple(Nullable(DateTime64(6, 'UTC')), UInt64))")
    specs["swim_latest_acc"] = {
        "description": "silver_ch.swim_latest: one row per SWIM flight_key, carrying the latest amendment as "
                       "latest_tuple (dep_point, dep_point_kind, arr_point, arr_point_kind, "
                       "filed_departure_time, filed_arrival_time, acid) — a positional contract dbt reads as "
                       ".1..7. Messages with no acid are excluded: acid is the callsign the filed plan is "
                       "matched to an airframe by, so such a row could never attach to a flight.",
        "db": "silver_ch",
        "view": "swim_latest",
        "target": f"""
CREATE TABLE IF NOT EXISTS silver_ch.swim_latest_acc
(
    flight_key   Nullable(String),
    latest_state {swim_state}
)
ENGINE = AggregatingMergeTree
ORDER BY flight_key
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS silver_ch.swim_latest_acc_mv
TO silver_ch.swim_latest_acc AS
{_SWIM_LATEST_SELECT}
""".strip(),
        # The seed's own bound, not the dbt model's 7 GB (#208): bootstrap aggregates the full 80M-row bronze
        # history in one INSERT (the model reads the ~4M-key -Merge view), so it must fail at a cap, not hang.
        "seed": [f"""
INSERT INTO silver_ch.swim_latest_acc
{_SWIM_LATEST_SELECT} SETTINGS max_memory_usage = 12000000000, max_execution_time = 900
""".strip()],
        "read": """
SELECT
    flight_key,
    argMaxMerge(latest_state) AS latest_tuple
FROM silver_ch.swim_latest_acc
GROUP BY flight_key
""".strip(),
    }

    return specs


SPECS = _spec()

def _db(spec: dict) -> str:
    return spec.get("db", "gold_ch")


def _serving_views(specs: dict) -> dict:
    # spec name -> (db, view base name, read SQL). Legacy specs carry neither key, so they keep mapping to
    # ("gold_ch", drop_old[0], read) — the Superset-facing names must not move when a new db-scoped spec lands.
    out = {}
    for name, spec in specs.items():
        base = spec.get("view") or (spec["drop_old"][0] if spec.get("drop_old") else None)
        if not base:
            # Fail loud at import (any test run catches it) — a nameless spec would silently serve nothing,
            # leaving consumers to read the opaque _acc state directly and count low.
            raise ValueError(f"ch_incremental_mvs: spec {name!r} has no serving-view name — "
                             "give it a 'view' key or a non-empty 'drop_old'")
        out[name] = (_db(spec), base, spec["read"])
    return out


# P5 Superset cutover: the view body IS the merge-aware read contract, so consumers (Superset, SQL Lab, dbt
# sources) read a plain table name and never touch the opaque *_state columns (which would count low).
SERVING_VIEWS = _serving_views(SPECS)


# Explicit per-target seed-completion ledger: a target's name appears here ONLY after all of its seed
# INSERTs succeeded. Gating on this (not on "table is non-empty") makes seeding retry-safe — a partial
# seed (e.g. live INSERT lands, adsblol INSERT fails) leaves the marker absent, so the Airflow retry
# truncates the partial rows and re-seeds in full instead of skipping the missing history. Single-writer:
# the ledger has no CH-side lock, so this relies on the init DAG's max_active_runs=1 (no concurrent apply).
_MARKER = "gold_ch.ch_mv_seeded"


def validate_names(raw, known) -> list[str]:
    # Scoping input must fail loudly, never degrade to "apply everything": an unscoped apply() DROP/CREATEs
    # every MV while the live insert lanes keep writing, and rows landing in that gap are lost forever.
    hint = f"; known: {sorted(known)}"
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"ch_incremental_mvs: 'names' must be a non-empty list of spec names, got {raw!r}{hint}")
    if not all(isinstance(n, str) for n in raw):
        raise ValueError(f"ch_incremental_mvs: 'names' must contain only spec-name strings, got {raw!r}{hint}")
    if len(set(raw)) != len(raw):
        raise ValueError(f"ch_incremental_mvs: 'names' has duplicates: {raw!r}{hint}")
    unknown = [n for n in raw if n not in known]
    if unknown:
        raise ValueError(f"ch_incremental_mvs: unknown spec name(s) {unknown!r}{hint}")
    return list(raw)


def _marker_present(client, name: str) -> bool:
    # The marker stays in gold_ch and is keyed by spec name alone (unambiguous across dbs).
    return bool(client.query(f"SELECT count() FROM {_MARKER} WHERE name = '{name}'").result_rows[0][0])


def _table_exists(client, db: str, name: str) -> bool:
    # Must be asked BEFORE the target's CREATE TABLE: it is the only marker-coherence signal a live MV can't
    # race — a row count can't, since one insert between CREATE and count() makes a stale marker look coherent.
    return bool(client.query(
        f"SELECT count() FROM system.tables WHERE database = '{db}' AND name = '{name}'"
    ).result_rows[0][0])


def _seed_once(client, name: str, spec: dict, *, force: bool, pre_existed: bool) -> tuple[int, bool]:
    # Marker-gated one-time seed from existing bronze (an MV sees only future INSERTs); retry-safe — marker out
    # first, re-marked only after every seed lands. force=True re-seeds a marked target. (rows_seeded, did_seed).
    db = _db(spec)
    if _marker_present(client, name) and not force:
        if pre_existed:
            return 0, False
        # Marker certifying a target that did not exist a moment ago (the documented rollback mistake: objects
        # dropped, marker left) — honoring it would serve an empty acc and skip its whole history forever.
        log.warning("ch_incremental_mvs %s: seed marker present but %s.%s was just created — marker is stale, "
                    "re-seeding", name, db, name)
    client.command(f"DELETE FROM {_MARKER} WHERE name = '{name}'")
    # Fail closed on the object itself: a seed that dies after the TRUNCATE must leave NO serving view, so
    # downstream errors loudly instead of reading a truncated acc as legitimately empty. Callers re-create it.
    view_base = spec.get("view") or (spec["drop_old"][0] if spec.get("drop_old") else None)
    if view_base:
        client.command(f"DROP VIEW IF EXISTS {db}.{view_base}")
    client.command(f"TRUNCATE TABLE {db}.{name}")
    for sql in spec.get("seed", ()):
        client.command(sql)
    seeded = client.query(f"SELECT count() FROM {db}.{name}").result_rows[0][0]
    client.command(f"INSERT INTO {_MARKER} (name) VALUES ('{name}')")
    return int(seeded), True


def apply(*, reseed: bool = False, names=None) -> dict:
    # Idempotent applier: drop superseded dbt CH tables, create the AggregatingMergeTree targets, (re)create
    # the MVs, then one-time-seed each target from existing bronze (MVs see only future INSERTs). The MV is
    # always DROP+CREATEd so a changed body deploys; the target uses IF NOT EXISTS so its data survives. names
    # scopes the run to specific specs (the v6.3 ADS-B re-grain migration drops + reseeds only the two adsb _acc,
    # leaving the OpenSky MVs untouched so there's no MV-recreate miss-window for the live states lane).
    from include.clickhouse import ch_client

    specs = SPECS if names is None else {n: SPECS[n] for n in validate_names(names, set(SPECS))}
    out: dict = {}
    client = ch_client()
    try:
        # gold_ch first (it holds the marker), then each selected spec's own db — a fresh bootstrap has neither.
        client.command("CREATE DATABASE IF NOT EXISTS gold_ch")
        client.command(f"CREATE TABLE IF NOT EXISTS {_MARKER} (name String) ENGINE = MergeTree ORDER BY name")
        for name, spec in specs.items():
            db = _db(spec)
            client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
            for old in spec.get("drop_old", ()):
                # drop_old removes the superseded dbt TABLE once; after P5 the base name is a serving VIEW
                # (recreated below), so skip it when it's already a view — CREATE OR REPLACE VIEW then swaps
                # atomically with no missing-view window for a live Superset reader during an init re-run.
                is_view = client.query(
                    f"SELECT engine LIKE '%View%' FROM system.tables "
                    f"WHERE database = '{db}' AND name = '{old}'"
                ).result_rows
                if is_view and is_view[0][0]:
                    continue
                client.command(f"DROP TABLE IF EXISTS {db}.{old}")
            pre_existed = _table_exists(client, db, name)
            client.command(spec["target"])
            client.command(f"DROP VIEW IF EXISTS {db}.{name}_mv")
            client.command(spec["mv"])
            seeded, did_seed = _seed_once(client, name, spec, force=reseed, pre_existed=pre_existed)
            out[name] = {"seeded_rows": seeded, "skipped_seed": not did_seed}
            log.info("ch_incremental_mvs %s: seeded_rows=%s skipped_seed=%s", name, seeded, not did_seed)
        # P5 serving views: each _acc's drop_old has already removed the old dbt table above, so CREATE OR
        # REPLACE VIEW gets a clean name; scoped to the processed specs so a names= run can't touch other views.
        # Load-bearing ordering: a _seed_once exception propagates out of the loop above and skips this block
        # entirely, so apply() also fails closed — it never publishes a view over a half-seeded acc.
        views = _serving_views(specs)
        for db, base, read_sql in views.values():
            client.command(f"CREATE OR REPLACE VIEW {db}.{base} AS {read_sql}")
            log.info("ch_incremental_mvs serving view %s.%s (re)created", db, base)
        out["serving_views"] = sorted(f"{db}.{base}" for db, base, _ in views.values())
    finally:
        client.close()
    return out


def ensure() -> dict:
    # Self-heal/bootstrap so a fresh deploy needs no manual init: create the _acc targets + MVs, marker-gated seed
    # from bronze, publish each certified spec's view. Never raises — it runs all_done, so a red tick still heals.
    from include.clickhouse import ch_client

    out: dict = {}
    try:
        client = ch_client()
    except Exception:
        log.exception("ch_incremental_mvs.ensure: client connect failed (non-fatal)")
        return {"ok": False}
    try:
        # gold_ch first (it holds the marker), then each spec's own db below — ensure() owns the fresh-bootstrap
        # promise, and a blank warehouse has neither database.
        client.command("CREATE DATABASE IF NOT EXISTS gold_ch")
        client.command(f"CREATE TABLE IF NOT EXISTS {_MARKER} (name String) ENGINE = MergeTree ORDER BY name")
        certified: set[str] = set()
        for name, spec in SPECS.items():
            try:
                db = _db(spec)
                client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
                pre_existed = _table_exists(client, db, name)
                client.command(spec["target"])   # CREATE TABLE IF NOT EXISTS
                # IF NOT EXISTS, never DROP+CREATE: apply()/the init DAG own body redeploys, so no per-tick
                # MV-recreate miss-window on the live insert lanes.
                client.command(spec["mv"])
                seeded, did_seed = _seed_once(client, name, spec, force=False, pre_existed=pre_existed)
                out[name] = {"seeded_rows": seeded, "skipped_seed": not did_seed}
                if _marker_present(client, name):
                    certified.add(name)
                else:
                    log.error("ch_incremental_mvs.ensure %s: seed marker absent after a clean block — "
                              "withholding its serving view", name)
            except Exception:
                log.exception("ch_incremental_mvs.ensure %s failed (non-fatal)", name)
                out[name] = {"error": True}
        # Fail closed: only an acc this run certified as fully seeded gets its view (re)published — a view over
        # an empty/partial acc reads as legitimately-empty and lets dbt build vacuously-green marts off it.
        for name, (db, base, read_sql) in SERVING_VIEWS.items():
            if name not in certified:
                log.warning("ch_incremental_mvs.ensure: %s not certified this run — leaving %s.%s as is",
                            name, db, base)
                continue
            try:
                client.command(f"CREATE OR REPLACE VIEW {db}.{base} AS {read_sql}")
            except Exception:
                log.exception("ch_incremental_mvs.ensure view %s.%s failed (non-fatal)", db, base)
    except Exception:
        # The per-spec/per-view blocks are guarded; this catches the rest (marker DDL, a missing gold_ch, a
        # permission/post-connect error) so the best-effort task never raises regardless of where it fails.
        log.exception("ch_incremental_mvs.ensure: aborted before completion (non-fatal)")
        out["ok"] = False
    finally:
        # Guard close() too — ensure() must never raise (best-effort task), and a close error would bubble.
        try:
            client.close()
        except Exception:
            log.exception("ch_incremental_mvs.ensure: client close failed (non-fatal)")
    return out


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--ensure" in sys.argv:
        print(json.dumps(ensure(), default=str, indent=2))
    else:
        # Same contract as the init DAG: apply/--reseed must name its scope. An accidental unscoped run
        # DROP/CREATEs every MV under the live insert lanes and rows in that gap are lost forever.
        arg = next((a for a in sys.argv[1:] if a.startswith("--names=")), None)
        if arg and "--all" in sys.argv:
            raise SystemExit("pass either --all or --names=<csv>, not both")
        if arg:
            cli_names = validate_names([n.strip() for n in arg.split("=", 1)[1].split(",") if n.strip()],
                                       set(SPECS))
        elif "--all" in sys.argv:
            cli_names = None
        else:
            raise SystemExit(f"apply requires --all or --names=<csv>; known: {sorted(SPECS)}")
        print(json.dumps(apply(reseed="--reseed" in sys.argv, names=cli_names), default=str, indent=2))
