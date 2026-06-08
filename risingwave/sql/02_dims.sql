-- Columns mirror the dbt seeds (silver is canonical); data is reloaded from the same
-- CSVs by the refresh_risingwave_dims DAG, so live and batch enrichment can't diverge.
CREATE TABLE IF NOT EXISTS dim_airlines (
    icao varchar,
    iata varchar,
    name varchar,
    callsign varchar,
    country varchar,
    active varchar
);

CREATE TABLE IF NOT EXISTS dim_hex_country (
    block_lo bigint,
    block_hi bigint,
    country varchar
);

-- dim_hex_country exploded to one row per overlapped 4096-address bucket: RW can't stream
-- non-equi joins, so the MV equi-joins on bucket with silver's BETWEEN as the residual.
CREATE TABLE IF NOT EXISTS dim_hex_country_buckets (
    bucket bigint,
    block_lo bigint,
    block_hi bigint,
    country varchar
);

-- ICAO Doc 8643 type → silhouette class; the MV equi-joins on typecode so livemap can draw
-- per-type icons (4-engine quad vs widebody vs narrowbody vs regional vs GA vs heli).
CREATE TABLE IF NOT EXISTS dim_aircraft_types (
    typecode varchar,
    engines integer,
    body_class varchar
);
