-- Bronze landing tables, all-Nullable to mirror the locked edge capture_v2 schema
-- (include/adsb_iceberg.py, include/iceberg.py, include/archive_iceberg.py,
-- include/flights_iceberg.py). All-Nullable is a hard contract: add_files can't promote
-- a nullable Parquet column, and the P2 s3()->INSERT type-matches the Parquet column for
-- column. ORDER BY columns are therefore Nullable, which needs allow_nullable_key=1
-- (MergeTree rejects nullable sort keys otherwise); the keys are never NULL in practice.

CREATE DATABASE IF NOT EXISTS bronze;

-- bronze.adsb_states — rooftop ADS-B (include/adsb_iceberg.py _build_schema):
-- capture_ts + 13 STRING + 17 DOUBLE + 23 INT(LongType->Int64) + 3 LIST + 1 JSON(acas_ra)
-- + _raw_json + _schema_version(IntegerType->Int32). capture_ts is a Float64 epoch second,
-- NOT a Date: partition via a MATERIALIZED Date that round-trips through toDateTime so the
-- epoch seconds aren't misread as days-since-epoch (a naive toDate would land rows in 1970).
CREATE TABLE IF NOT EXISTS bronze.adsb_states
(
    capture_ts      Nullable(Float64),
    -- STRING fields (_STRING_FIELDS)
    hex Nullable(String), type Nullable(String), r Nullable(String), t Nullable(String),
    `desc` Nullable(String), category Nullable(String), sil_type Nullable(String),
    emergency Nullable(String), ownOp Nullable(String), year Nullable(String),
    flight Nullable(String), squawk Nullable(String), alt_baro Nullable(String),
    -- DOUBLE fields (_DOUBLE_FIELDS)
    now Nullable(Float64), lat Nullable(Float64), lon Nullable(Float64),
    r_dst Nullable(Float64), r_dir Nullable(Float64), seen Nullable(Float64),
    seen_pos Nullable(Float64), rssi Nullable(Float64), gs Nullable(Float64),
    mach Nullable(Float64), track Nullable(Float64), track_rate Nullable(Float64),
    roll Nullable(Float64), mag_heading Nullable(Float64), true_heading Nullable(Float64),
    nav_qnh Nullable(Float64), nav_heading Nullable(Float64),
    -- INT fields (_INT_FIELDS) — Iceberg LongType -> Int64
    messages Nullable(Int64), nic Nullable(Int64), rc Nullable(Int64), version Nullable(Int64),
    nac_p Nullable(Int64), nac_v Nullable(Int64), sil Nullable(Int64), nic_baro Nullable(Int64),
    gva Nullable(Int64), sda Nullable(Int64), alert Nullable(Int64), spi Nullable(Int64),
    alt_geom Nullable(Int64), ias Nullable(Int64), tas Nullable(Int64), baro_rate Nullable(Int64),
    geom_rate Nullable(Int64), nav_altitude_mcp Nullable(Int64), nav_altitude_fms Nullable(Int64),
    wd Nullable(Int64), ws Nullable(Int64), oat Nullable(Int64), tat Nullable(Int64),
    -- LIST fields (_LIST_FIELDS) — element-nullable list of string (element_required=False)
    nav_modes Array(Nullable(String)), mlat Array(Nullable(String)), tisb Array(Nullable(String)),
    -- JSON field (_JSON_FIELDS)
    acas_ra Nullable(String),
    -- provenance / drift columns
    _raw_json Nullable(String),          -- ~39% of on-disk in the spike; P8 relocates to NAS cold tier
    _schema_version Nullable(Int32),
    -- partition driver: capture_ts is Float64 epoch sec, NOT a Date
    capture_date Date MATERIALIZED toDate(toDateTime(capture_ts))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(capture_date)
ORDER BY capture_ts
SETTINGS index_granularity = 8192, allow_nullable_key = 1;

-- bronze.opensky_states — OpenSky /states (include/iceberg.py SCHEMA fields 1-20):
-- Timestamptz -> DateTime64(6,'UTC'), Boolean -> Bool, Integer -> Int32.
CREATE TABLE IF NOT EXISTS bronze.opensky_states
(
    icao24 Nullable(String), callsign Nullable(String), origin_country Nullable(String),
    time_position Nullable(DateTime64(6,'UTC')), last_contact Nullable(DateTime64(6,'UTC')),
    longitude Nullable(Float64), latitude Nullable(Float64), baro_altitude Nullable(Float64),
    on_ground Nullable(Bool), velocity Nullable(Float64), true_track Nullable(Float64),
    vertical_rate Nullable(Float64), geo_altitude Nullable(Float64), squawk Nullable(String),
    spi Nullable(Bool), position_source Nullable(Int32),
    snapshot_time Nullable(DateTime64(6,'UTC')), region Nullable(String),
    ingested_at Nullable(DateTime64(6,'UTC')), committed_at Nullable(DateTime64(6,'UTC')),
    snapshot_date Date MATERIALIZED toDate(snapshot_time),
    -- P8a dedup key: a crash-window whole-file/partial replay differs ONLY in load-time committed_at, so a
    -- fingerprint over every SOURCE column EXCEPT committed_at collapses replays under RMT while leaving the
    -- ~374K legit content-distinct same-grain recaptures (distinct fp) intact. Cols = STATES_COLUMNS minus
    -- committed_at (test_bronze_dedup guards drift); toString(tuple()) flattens the all-Nullable schema so a
    -- NULL doesn't poison the hash. committed_at-exclusion is safe ONLY because the source Parquet has no
    -- committed_at column (so the only rows this can collapse are loader replays).
    _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(icao24, callsign, origin_country, time_position,
        last_contact, longitude, latitude, baro_altitude, on_ground, velocity, true_track, vertical_rate,
        geo_altitude, squawk, spi, position_source, snapshot_time, region, ingested_at)))
)
-- No version column (keep-arbitrary): committed_at is Nullable so it can't be an RMT version, and it doesn't
-- need to be — replay twins share the full ORDER BY key and are identical in every column except committed_at,
-- so which one survives the merge is immaterial.
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (snapshot_time, icao24, _dedup_fp)
PRIMARY KEY (snapshot_time, icao24)
SETTINGS allow_nullable_key = 1;

-- bronze.archive_states — opensky_states fields 1-20 + source (field 21; ODbL adsb.lol
-- provenance, include/archive_iceberg.py). Same partition/ORDER BY so dbt history unions
-- with the live lane column-for-column.
CREATE TABLE IF NOT EXISTS bronze.archive_states
(
    icao24 Nullable(String), callsign Nullable(String), origin_country Nullable(String),
    time_position Nullable(DateTime64(6,'UTC')), last_contact Nullable(DateTime64(6,'UTC')),
    longitude Nullable(Float64), latitude Nullable(Float64), baro_altitude Nullable(Float64),
    on_ground Nullable(Bool), velocity Nullable(Float64), true_track Nullable(Float64),
    vertical_rate Nullable(Float64), geo_altitude Nullable(Float64), squawk Nullable(String),
    spi Nullable(Bool), position_source Nullable(Int32),
    snapshot_time Nullable(DateTime64(6,'UTC')), region Nullable(String),
    ingested_at Nullable(DateTime64(6,'UTC')), committed_at Nullable(DateTime64(6,'UTC')),
    source Nullable(String),
    snapshot_date Date MATERIALIZED toDate(snapshot_time)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (snapshot_time, icao24)
SETTINGS allow_nullable_key = 1;

-- bronze.opensky_flights — OpenSky /flights (include/flights_iceberg.py FLIGHTS_SCHEMA
-- fields 1-12). flight_duration_seconds is IntegerType -> Int32.
CREATE TABLE IF NOT EXISTS bronze.opensky_flights
(
    icao24 Nullable(String), callsign Nullable(String),
    first_seen Nullable(DateTime64(6,'UTC')), last_seen Nullable(DateTime64(6,'UTC')),
    est_departure_airport Nullable(String), est_arrival_airport Nullable(String),
    flight_duration_seconds Nullable(Int32), captured_for_airport Nullable(String),
    direction Nullable(String), window_kind Nullable(String),
    ingested_at Nullable(DateTime64(6,'UTC')), committed_at Nullable(DateTime64(6,'UTC')),
    -- first_seen is nullable in practice (a flight summary can lack it; Iceberg keeps it in a null
    -- partition), so fold NULL into the 1970 partition — a non-nullable Date can't hold NULL.
    first_seen_date Date MATERIALIZED ifNull(toDate(first_seen), toDate(0))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(first_seen_date)
ORDER BY (first_seen, icao24)
SETTINGS allow_nullable_key = 1;

-- bronze.aircraft_db — OpenSky aircraft database subset (include/flights_iceberg.py
-- AIRCRAFT_DB_SCHEMA fields 1-15). as_of_date is a real DateType column (field 13).
CREATE TABLE IF NOT EXISTS bronze.aircraft_db
(
    icao24 Nullable(String), registration Nullable(String), manufacturericao Nullable(String),
    manufacturername Nullable(String), model Nullable(String), typecode Nullable(String),
    serialnumber Nullable(String), icaoaircrafttype Nullable(String), operator Nullable(String),
    operatorcallsign Nullable(String), operatoricao Nullable(String), owner Nullable(String),
    as_of_date Nullable(Date),
    ingested_at Nullable(DateTime64(6,'UTC')), committed_at Nullable(DateTime64(6,'UTC'))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(as_of_date)
ORDER BY icao24
SETTINGS allow_nullable_key = 1;
