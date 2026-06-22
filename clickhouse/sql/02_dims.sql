-- Dim tables mirroring the four dbt seeds (dbt/sancha1090/seeds/*.csv headers). Empty here;
-- loaded in P3. Plain non-Nullable MergeTree keyed on the natural id. dim_hex_country backs
-- the range_hashed dictionary in 03_dict_hex_country.sql.

CREATE DATABASE IF NOT EXISTS dim;

-- dim_hex_country.csv: block_lo,block_hi,country (214 ranges + header)
CREATE TABLE IF NOT EXISTS dim.dim_hex_country
(block_lo UInt32, block_hi UInt32, country String)
ENGINE = MergeTree ORDER BY block_lo;

-- dim_airlines.csv: icao,iata,name,callsign,country,active
CREATE TABLE IF NOT EXISTS dim.dim_airlines
(icao String, iata String, name String, callsign String, country String, active String)
ENGINE = MergeTree ORDER BY icao;

-- dim_airports.csv: icao,iata,name,city,country,lat,lon
CREATE TABLE IF NOT EXISTS dim.dim_airports
(icao String, iata String, name String, city String, country String, lat Float64, lon Float64)
ENGINE = MergeTree ORDER BY icao;

-- dim_aircraft_types.csv: typecode,engines,body_class,model_name
CREATE TABLE IF NOT EXISTS dim.dim_aircraft_types
(typecode String, engines String, body_class String, model_name String)
ENGINE = MergeTree ORDER BY typecode;
