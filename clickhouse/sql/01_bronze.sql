-- Bronze landing tables, all-Nullable to mirror the locked edge capture_v2 schema
-- (include/adsb_iceberg.py, include/iceberg.py, include/archive_iceberg.py,
-- include/flights_iceberg.py). All-Nullable is a hard contract: add_files can't promote
-- a nullable Parquet column, and the P2 s3()->INSERT type-matches the Parquet column for
-- column. ORDER BY columns are therefore Nullable, which needs allow_nullable_key=1
-- (MergeTree rejects nullable sort keys otherwise); the keys are never NULL in practice.

CREATE DATABASE IF NOT EXISTS bronze;

-- bronze.adsb_states — rooftop ADS-B (include/adsb_schema.py CH_ADSB_COLUMNS):
-- capture_ts + 13 STRING + 17 DOUBLE + 23 INT(Int64) + 3 LIST + 1 JSON(acas_ra) + db_flags + _schema_version.
-- v6.3: _raw_json ELIMINATED from CH (was ~39% of on-disk; the only CH readers decoded dbFlags). The verbatim
-- blob stays in the source Garage/NAS Parquet (the drift scan reads it there); db_flags is the dbFlags integer
-- baked at load (absent=0, the JSONExtractInt 2-valued contract), so silver/MV read a typed column not JSON.
-- capture_ts is a Float64 epoch second, NOT a Date: partition via a MATERIALIZED Date that round-trips through
-- toDateTime so the epoch seconds aren't misread as days-since-epoch (a naive toDate lands rows in 1970).
-- Codecs (live 2M-row test): Gorilla/DoubleDelta REGRESS here (sorted by capture_ts globally -> adjacent rows
-- are different aircraft -> floats uncorrelated), so ZSTD(3) wins on capture_ts/floats/strings; T64+ZSTD(3) wins
-- on the bounded Int64 fields (~2x over DoubleDelta). RMT: (hex,capture_ts) is fully unique == content and a
-- crash-replay re-insert is byte-identical on every column (no committed_at like opensky_states), so a no-version
-- ReplacingMergeTree ORDER BY (capture_ts, hex) collapses identical-key replays with no _dedup_fp; capture_ts
-- stays first so the gate's capture_ts<cutoff PK-prunes.
CREATE TABLE IF NOT EXISTS bronze.adsb_states
(
    capture_ts       Nullable(Float64)       CODEC(ZSTD(3)),
    -- STRING fields (include/adsb_schema.py STRING_FIELDS)
    hex              Nullable(String)        CODEC(ZSTD(3)),
    type             Nullable(String)        CODEC(ZSTD(3)),
    r                Nullable(String)        CODEC(ZSTD(3)),
    t                Nullable(String)        CODEC(ZSTD(3)),
    `desc`           Nullable(String)        CODEC(ZSTD(3)),
    category         Nullable(String)        CODEC(ZSTD(3)),
    sil_type         Nullable(String)        CODEC(ZSTD(3)),
    emergency        Nullable(String)        CODEC(ZSTD(3)),
    ownOp            Nullable(String)        CODEC(ZSTD(3)),
    year             Nullable(String)        CODEC(ZSTD(3)),
    flight           Nullable(String)        CODEC(ZSTD(3)),
    squawk           Nullable(String)        CODEC(ZSTD(3)),
    alt_baro         Nullable(String)        CODEC(ZSTD(3)),
    -- DOUBLE fields (include/adsb_schema.py DOUBLE_FIELDS)
    now              Nullable(Float64)       CODEC(ZSTD(3)),
    lat              Nullable(Float64)       CODEC(ZSTD(3)),
    lon              Nullable(Float64)       CODEC(ZSTD(3)),
    r_dst            Nullable(Float64)       CODEC(ZSTD(3)),
    r_dir            Nullable(Float64)       CODEC(ZSTD(3)),
    seen             Nullable(Float64)       CODEC(ZSTD(3)),
    seen_pos         Nullable(Float64)       CODEC(ZSTD(3)),
    rssi             Nullable(Float64)       CODEC(ZSTD(3)),
    gs               Nullable(Float64)       CODEC(ZSTD(3)),
    mach             Nullable(Float64)       CODEC(ZSTD(3)),
    track            Nullable(Float64)       CODEC(ZSTD(3)),
    track_rate       Nullable(Float64)       CODEC(ZSTD(3)),
    roll             Nullable(Float64)       CODEC(ZSTD(3)),
    mag_heading      Nullable(Float64)       CODEC(ZSTD(3)),
    true_heading     Nullable(Float64)       CODEC(ZSTD(3)),
    nav_qnh          Nullable(Float64)       CODEC(ZSTD(3)),
    nav_heading      Nullable(Float64)       CODEC(ZSTD(3)),
    -- INT fields (include/adsb_schema.py INT_FIELDS) — T64 exploits the shared bit-width of bounded ints
    messages         Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nic              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    rc               Nullable(Int64)         CODEC(T64, ZSTD(3)),
    version          Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nac_p            Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nac_v            Nullable(Int64)         CODEC(T64, ZSTD(3)),
    sil              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nic_baro         Nullable(Int64)         CODEC(T64, ZSTD(3)),
    gva              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    sda              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    alert            Nullable(Int64)         CODEC(T64, ZSTD(3)),
    spi              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    alt_geom         Nullable(Int64)         CODEC(T64, ZSTD(3)),
    ias              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    tas              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    baro_rate        Nullable(Int64)         CODEC(T64, ZSTD(3)),
    geom_rate        Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nav_altitude_mcp Nullable(Int64)         CODEC(T64, ZSTD(3)),
    nav_altitude_fms Nullable(Int64)         CODEC(T64, ZSTD(3)),
    wd               Nullable(Int64)         CODEC(T64, ZSTD(3)),
    ws               Nullable(Int64)         CODEC(T64, ZSTD(3)),
    oat              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    tat              Nullable(Int64)         CODEC(T64, ZSTD(3)),
    -- LIST fields (include/adsb_schema.py LIST_FIELDS) — element-nullable list of string
    nav_modes        Array(Nullable(String)) CODEC(ZSTD(3)),
    mlat             Array(Nullable(String)) CODEC(ZSTD(3)),
    tisb             Array(Nullable(String)) CODEC(ZSTD(3)),
    -- JSON field (include/adsb_schema.py JSON_FIELDS)
    acas_ra          Nullable(String)        CODEC(ZSTD(3)),
    -- provenance: db_flags = the dbFlags integer decoded from JSON at load (absent=0); replaces _raw_json
    db_flags         Int32 DEFAULT 0         CODEC(ZSTD(3)),
    _schema_version  Nullable(Int32)         CODEC(ZSTD(3)),
    -- partition driver: capture_ts is Float64 epoch sec (NOT a Date)
    capture_date Date MATERIALIZED toDate(toDateTime(capture_ts))
)
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(capture_date)
ORDER BY (capture_ts, hex)
PRIMARY KEY capture_ts
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

-- bronze.adsblol_states — opensky_states fields 1-20 + source (field 21; ODbL adsb.lol provenance,
-- include/adsblol_backfill.py). Same partition/ORDER BY so the dbt adsblol history unions with the
-- live lane column-for-column.
CREATE TABLE IF NOT EXISTS bronze.adsblol_states
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
