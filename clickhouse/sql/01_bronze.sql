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
SETTINGS index_granularity = 8192, allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

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
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

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
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

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
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

-- bronze.adsblol_flight_segments — global flight segments from adsb.lol full traces
-- (include/adsblol_routes.py); RMT versioned on ingested_at so a refetched trace upserts.
CREATE TABLE IF NOT EXISTS bronze.adsblol_flight_segments
(
    icao24 Nullable(String), callsign Nullable(String),
    seg_start Nullable(DateTime64(6,'UTC')), seg_end Nullable(DateTime64(6,'UTC')),
    num_fixes Nullable(Int64),
    first_lat Nullable(Float64), first_lon Nullable(Float64),
    first_alt_ft Nullable(Float64), first_on_ground Nullable(Bool),
    last_lat Nullable(Float64), last_lon Nullable(Float64),
    last_alt_ft Nullable(Float64), last_on_ground Nullable(Bool),
    trace_day Date, source Nullable(String),
    ingested_at DateTime64(6,'UTC'), committed_at Nullable(DateTime64(6,'UTC'))
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(trace_day)
ORDER BY (trace_day, icao24, seg_start)
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

-- bronze.adsblol_flight_paths — full path points of the kept segments; read by
-- scripts/backfill_adsblol_resegment.py's gap/altitude scan to find re-segmentation targets.
CREATE TABLE IF NOT EXISTS bronze.adsblol_flight_paths
(
    icao24 Nullable(String), seg_start Nullable(DateTime64(6,'UTC')),
    ts Nullable(DateTime64(6,'UTC')),
    lat Nullable(Float64), lon Nullable(Float64), alt_ft Nullable(Float64),
    on_ground Nullable(Bool), gs_kt Nullable(Float64), track_deg Nullable(Float64),
    trace_day Date, source Nullable(String),
    ingested_at DateTime64(6,'UTC'), committed_at Nullable(DateTime64(6,'UTC'))
)
ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMM(trace_day)
ORDER BY (trace_day, icao24, ts)
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;

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

-- bronze.adsbx_aircraft_db — ADSBExchange basic-ac-db weekly snapshots: type/identity fill for
-- dim_aircraft_registry blanks. ownop = FAA registrant (owner-shaped only); faa_* / mil land unconsumed.
CREATE TABLE IF NOT EXISTS bronze.adsbx_aircraft_db
(
    icao24 Nullable(String), registration Nullable(String), icaotype Nullable(String),
    short_type Nullable(String), year Nullable(UInt16),
    manufacturer Nullable(String), model Nullable(String), ownop Nullable(String),
    faa_pia Nullable(UInt8), faa_ladd Nullable(UInt8), mil Nullable(UInt8),
    as_of_date Nullable(Date),
    ingested_at Nullable(DateTime64(6,'UTC')), committed_at Nullable(DateTime64(6,'UTC'))
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(as_of_date)
ORDER BY icao24
SETTINGS allow_nullable_key = 1;

-- bronze.swim_flightdata — FAA SWIM TFMData fltdMessage (SP3a). Append log: RMT collapses same-partition exact
-- replays only; latest-amendment selection is a dbt argMax concern, not the engine. _dedup_fp + ORDER BY EXCLUDE
-- the volatile receive/flush stamps (source_received_at, ingested_at) or a redelivery wouldn't collapse.
CREATE TABLE IF NOT EXISTS bronze.swim_flightdata
(
    gufi Nullable(String), flight_ref Nullable(String), acid Nullable(String), computer_id Nullable(String),
    msg_type Nullable(String),
    dep_point Nullable(String), dep_point_kind Nullable(String), dep_point_raw Nullable(String),
    arr_point Nullable(String), arr_point_kind Nullable(String), arr_point_raw Nullable(String),
    filed_departure_time Nullable(DateTime64(6,'UTC')), filed_departure_time_raw Nullable(String),
    filed_arrival_time Nullable(DateTime64(6,'UTC')), filed_arrival_time_raw Nullable(String),
    msg_timestamp Nullable(DateTime64(6,'UTC')),
    source_received_at Nullable(DateTime64(6,'UTC')), ingested_at Nullable(DateTime64(6,'UTC')),
    raw_xml Nullable(String),
    -- partition on the INTRINSIC message time (replay-stable): a redelivery re-derives the same partition, so
    -- RMT twins always meet — receive-time would split twins across a month boundary. NULL folds to 1970.
    swim_date Date MATERIALIZED ifNull(toDate(msg_timestamp), toDate(0)),
    -- content fingerprint over every column EXCEPT the volatile stamps (source_received_at, ingested_at). A
    -- HASH of raw_xml is included (not the bulky string) so an amendment differing only in an UNPARSED field
    -- still gets a distinct fp and survives — parsed fields alone would wrongly collapse it. toString(tuple())
    -- so a NULL can't poison the hash. The compact _dedup_fp (not raw_xml) is what goes in ORDER BY.
    _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(gufi, flight_ref, acid, computer_id, msg_type,
        dep_point, dep_point_kind, dep_point_raw, arr_point, arr_point_kind, arr_point_raw,
        filed_departure_time, filed_departure_time_raw, filed_arrival_time, filed_arrival_time_raw, msg_timestamp,
        cityHash64(raw_xml))))
)
ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(swim_date)
-- version = msg_timestamp (@sourceTimeStamp, present on 100% of messages, intrinsic + monotonic → redelivery-safe,
-- spike-confirmed 2026-07-08). Volatile stamps deliberately absent from ORDER BY: replays differ only in them,
-- so exact twins share this key and collapse.
ORDER BY (msg_timestamp, acid, _dedup_fp)
PRIMARY KEY (msg_timestamp, acid)
SETTINGS allow_nullable_key = 1, fsync_after_insert = 1, fsync_part_directory = 1;
