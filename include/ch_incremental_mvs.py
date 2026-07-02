from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# P4 cheap aggregates as self-maintaining AggregatingMergeTree MVs; shadow state until P5 (full design:
# opensky-docs/relevant/2026-06-20-ch-migration-p4-parity-results.md). Three invariants the code depends on:
# READS = the merge-aware read contract; raw *_state columns are opaque (uniqExactMerge/sum + GROUP BY only).
# MVs attach to append-only bronze, never the dbt-REPLACE'd silver (an MV can't survive a drop+recreate).
# Context-lane obs = uniqExact((icao24,snapshot_time)): dedup-immune (an MV can't dedup across blocks),
# where ADS-B is row-preserving over bronze so plain count() suffices.

_OPENSKY = "bronze.opensky_states"
_ARCHIVE = "bronze.archive_states"
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
_ADSB_AIRLINE_MV_BODY = f"""
SELECT
    toStartOfHour(toDateTime(t.capture_ts))       AS snapshot_hour,
    al.name                                       AS airline_name,
    al.country                                    AS airline_country,
    uniqExactState(t.hex)                         AS distinct_aircraft_state,
    uniqExactState((t.hex, t.capture_ts))         AS observations,
    uniqExactStateIf((t.hex, t.capture_ts), t.callsign_source = 'opensky_backfill') AS backfilled_observations
FROM (
    SELECT
        s.hex AS hex,
        s.capture_ts AS capture_ts,
        coalesce(nullIf(trimBoth(any(s.flight)), ''),
                 argMinIf(o.callsign, (abs(o.snap_epoch - s.capture_ts), -o.snap_epoch, o.callsign),
                          o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S})) AS callsign_filled,
        multiIf(nullIf(trimBoth(any(s.flight)), '') IS NOT NULL, 'adsb',
                countIf(o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S}) > 0,
                'opensky_backfill', NULL)         AS callsign_source
    FROM {_ADSB} s
    LEFT JOIN ({_OPENSKY_CALLSIGN}) o ON o.icao24 = s.hex
    GROUP BY s.hex, s.capture_ts
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
    return f"""
SELECT
    coalesce(al.name, '')                                    AS airline_name,
    coalesce(al.country, '')                                 AS airline_country,
    toUnixTimestamp(toStartOfHour(toDateTime(t.capture_ts))) AS h,
    uniqExact(t.hex)                                         AS distinct_aircraft,
    uniqExact((t.hex, t.capture_ts))                        AS observations,
    uniqExactIf((t.hex, t.capture_ts), t.callsign_source = 'opensky_backfill') AS backfilled
FROM (
    SELECT
        s.hex AS hex,
        s.capture_ts AS capture_ts,
        coalesce(nullIf(trimBoth(any(s.flight)), ''),
                 argMinIf(o.callsign, (abs(o.snap_epoch - s.capture_ts), -o.snap_epoch, o.callsign),
                          o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S})) AS callsign_filled,
        multiIf(nullIf(trimBoth(any(s.flight)), '') IS NOT NULL, 'adsb',
                countIf(o.callsign IS NOT NULL AND abs(o.snap_epoch - s.capture_ts) <= {_BF_WINDOW_S}) > 0,
                'opensky_backfill', NULL)                    AS callsign_source
    FROM {_ADSB} s
    LEFT JOIN ({opensky_win}) o ON o.icao24 = s.hex
    WHERE s.capture_ts >= {lo} AND s.capture_ts < {hi}
    GROUP BY s.hex, s.capture_ts
) t
{_AL_JOIN.format(dim=_DIM_AIRLINES, cs="t.callsign_filled")}
GROUP BY airline_name, airline_country, h
""".strip()


def _spec():
    # Each spec: target DDL + MV DDL + one-time seed INSERTs + the merge-aware read (parity / P5 view).
    obs_tuple = "AggregateFunction(uniqExact, Tuple(Nullable(String), Nullable(DateTime64(6, 'UTC'))))"
    uniq_str = "AggregateFunction(uniqExact, Nullable(String))"
    # ADS-B obs grain is now (group, hour) (v6.3 re-grain) so uniqExact is affordable AND exact: each
    # (hex, capture_ts) lands in one disjoint hour, so uniqExactMerge over a group's hours == the group total;
    # replay-immune (merge unions states) and bounded by a 90d TTL. capture_ts is Float64 epoch.
    adsb_obs = "AggregateFunction(uniqExact, Tuple(Nullable(String), Nullable(Float64)))"

    specs = {}

    # 1) Hourly traffic — accumulate-forever (replaces agg_hourly_traffic{,_history,_live_archive}).
    specs["agg_hourly_traffic_acc"] = {
        "drop_old": ["agg_hourly_traffic", "agg_hourly_traffic_history", "agg_hourly_traffic_live_archive"],
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
        # Live = all bronze history; history = archive hours strictly below the live floor (disjoint, no
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
FROM {_ARCHIVE}
WHERE region = 'japan' AND latitude IS NOT NULL AND longitude IS NOT NULL
GROUP BY snapshot_hour
-- coalesce: an empty live lane (blank-warehouse bootstrap) has a NULL floor; seed ALL archive hours then.
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

    # 2) Airline traffic (OpenSky context) — hourly grain. NOT accumulate-forever: the dbt mart reads
    # the 30-day-windowed fact_state_snapshots, so the read applies a 30-day filter for parity.
    al_join = _AL_JOIN.format(dim=_DIM_AIRLINES, cs="s.callsign")
    specs["agg_airline_traffic_acc"] = {
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
""".strip(),
        "mv": f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS gold_ch.agg_airline_traffic_acc_mv
TO gold_ch.agg_airline_traffic_acc AS
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
""".strip(),
        "seed": [f"""
INSERT INTO gold_ch.agg_airline_traffic_acc
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
""".strip()],
        "read": """
SELECT
    snapshot_hour, airline_name, airline_country,
    uniqExactMerge(distinct_aircraft_state) AS distinct_aircraft,
    uniqExactMerge(observations_state)      AS observations
FROM gold_ch.agg_airline_traffic_acc
WHERE snapshot_hour >= now('UTC') - INTERVAL 30 DAY
GROUP BY snapshot_hour, airline_name, airline_country
ORDER BY snapshot_hour, distinct_aircraft DESC
""".strip(),
    }

    # 3) Airline traffic (rooftop ADS-B) — two-sided OpenSky callsign backfill (see _ADSB_AIRLINE_*_BODY).
    specs["agg_airline_traffic_adsb_acc"] = {
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

    return specs


SPECS = _spec()
# Read contract (uniqExactMerge/sum + GROUP BY) for the serving views — never read the _acc state columns raw.
READS = {name: spec["read"] for name, spec in SPECS.items()}

# P5 Superset cutover: expose each _acc's merge-aware read as a named gold_ch view, keyed by the original
# mart name (drop_old[0]). P4 deferred this ("expose reads only through named views in P5"): consumers
# (Superset datasets, SQL Lab) then read a plain table name and never touch the opaque *_state columns. The
# body IS the READS contract, so a consumer reading through the view gets the merge-aware aggregate.
SERVING_VIEWS = {spec["drop_old"][0]: spec["read"] for spec in SPECS.values() if spec.get("drop_old")}


# Explicit per-target seed-completion ledger: a target's name appears here ONLY after all of its seed
# INSERTs succeeded. Gating on this (not on "table is non-empty") makes seeding retry-safe — a partial
# seed (e.g. live INSERT lands, archive INSERT fails) leaves the marker absent, so the Airflow retry
# truncates the partial rows and re-seeds in full instead of skipping the missing history. Single-writer:
# the ledger has no CH-side lock, so this relies on the init DAG's max_active_runs=1 (no concurrent apply).
_MARKER = "gold_ch.ch_mv_seeded"


def _seed_once(client, name: str, spec: dict, *, force: bool) -> tuple[int, bool]:
    # Marker-gated one-time seed from existing bronze: an MV sees only future INSERTs, so the rows already in
    # bronze when it is created must be seeded explicitly. Retry-safe ordering — invalidate the marker BEFORE
    # truncating and re-mark only AFTER every seed succeeds, so a partial seed leaves the marker absent and the
    # next run re-seeds. force=True re-seeds an already-marked target. Returns (rows_seeded, did_seed).
    done = client.query(f"SELECT count() FROM {_MARKER} WHERE name = '{name}'").result_rows[0][0]
    if done and not force:
        return 0, False
    client.command(f"DELETE FROM {_MARKER} WHERE name = '{name}'")
    client.command(f"TRUNCATE TABLE gold_ch.{name}")
    for sql in spec["seed"]:
        client.command(sql)
    seeded = client.query(f"SELECT count() FROM gold_ch.{name}").result_rows[0][0]
    client.command(f"INSERT INTO {_MARKER} (name) VALUES ('{name}')")
    return int(seeded), True


def apply(*, reseed: bool = False, names=None) -> dict:
    # Idempotent applier: drop superseded dbt CH tables, create the AggregatingMergeTree targets, (re)create
    # the MVs, then one-time-seed each target from existing bronze (MVs see only future INSERTs). The MV is
    # always DROP+CREATEd so a changed body deploys; the target uses IF NOT EXISTS so its data survives. names
    # scopes the run to specific specs (the v6.3 ADS-B re-grain migration drops + reseeds only the two adsb _acc,
    # leaving the OpenSky MVs untouched so there's no MV-recreate miss-window for the live states lane).
    from include.clickhouse import ch_client

    specs = SPECS if names is None else {n: SPECS[n] for n in names}
    out: dict = {}
    client = ch_client()
    try:
        client.command(f"CREATE TABLE IF NOT EXISTS {_MARKER} (name String) ENGINE = MergeTree ORDER BY name")
        for name, spec in specs.items():
            for old in spec.get("drop_old", ()):
                # drop_old removes the superseded dbt TABLE once; after P5 the base name is a serving VIEW
                # (recreated below), so skip it when it's already a view — CREATE OR REPLACE VIEW then swaps
                # atomically with no missing-view window for a live Superset reader during an init re-run.
                is_view = client.query(
                    f"SELECT engine LIKE '%View%' FROM system.tables "
                    f"WHERE database = 'gold_ch' AND name = '{old}'"
                ).result_rows
                if is_view and is_view[0][0]:
                    continue
                client.command(f"DROP TABLE IF EXISTS gold_ch.{old}")
            client.command(spec["target"])
            client.command(f"DROP VIEW IF EXISTS gold_ch.{name}_mv")
            client.command(spec["mv"])
            seeded, did_seed = _seed_once(client, name, spec, force=reseed)
            out[name] = {"seeded_rows": seeded, "skipped_seed": not did_seed}
            log.info("ch_incremental_mvs %s: seeded_rows=%s skipped_seed=%s", name, seeded, not did_seed)
        # P5 serving views: each _acc's drop_old has already removed the old dbt table above, so CREATE OR
        # REPLACE VIEW gets a clean name. The view body is the merge-aware READS contract (no opaque state).
        # Recreate only the processed specs' serving views so a scoped (names=) run can't touch other views.
        views = {spec["drop_old"][0]: spec["read"] for spec in specs.values() if spec.get("drop_old")}
        for base, read_sql in views.items():
            client.command(f"CREATE OR REPLACE VIEW gold_ch.{base} AS {read_sql}")
            log.info("ch_incremental_mvs serving view gold_ch.%s (re)created", base)
        out["serving_views"] = sorted(views)
    finally:
        client.close()
    return out


def ensure() -> dict:
    # Self-heal/bootstrap so a fresh deploy needs no manual init before Superset reads CH: idempotently create
    # the _acc targets + MVs (IF NOT EXISTS), one-time-seed each from existing bronze (marker-gated, so the rows
    # ingested before the MV existed aren't lost and an already-seeded target is left untouched), then (re)create
    # the serving views. The MV is NOT dropped/recreated here (apply()/the init DAG own body redeploys), so there
    # is no per-tick MV-recreate miss-window. Best-effort: never raises, so it can be a non-blocking transform step.
    from include.clickhouse import ch_client

    out: dict = {}
    try:
        client = ch_client()
    except Exception:
        log.exception("ch_incremental_mvs.ensure: client connect failed (non-fatal)")
        return {"ok": False}
    try:
        client.command(f"CREATE TABLE IF NOT EXISTS {_MARKER} (name String) ENGINE = MergeTree ORDER BY name")
        for name, spec in SPECS.items():
            try:
                client.command(spec["target"])   # CREATE TABLE IF NOT EXISTS
                client.command(spec["mv"])        # CREATE MATERIALIZED VIEW IF NOT EXISTS
                seeded, did_seed = _seed_once(client, name, spec, force=False)
                out[name] = {"seeded_rows": seeded, "skipped_seed": not did_seed}
            except Exception:
                log.exception("ch_incremental_mvs.ensure %s failed (non-fatal)", name)
                out[name] = {"error": True}
        for base, read_sql in SERVING_VIEWS.items():
            try:
                client.command(f"CREATE OR REPLACE VIEW gold_ch.{base} AS {read_sql}")
            except Exception:
                log.exception("ch_incremental_mvs.ensure view gold_ch.%s failed (non-fatal)", base)
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
        print(json.dumps(apply(reseed="--reseed" in sys.argv), default=str, indent=2))
