-- dim_hex_country backs the range_hashed dictionary in 03_dict_hex_country.sql. Empty here; loaded in P3.

CREATE DATABASE IF NOT EXISTS dim;

-- dim_hex_country.csv: block_lo,block_hi,country (214 ranges + header)
CREATE TABLE IF NOT EXISTS dim.dim_hex_country
(block_lo UInt32, block_hi UInt32, country String)
ENGINE = MergeTree ORDER BY block_lo;

-- dim_ladd — FAA LADD privacy-list SCD2 (SP3b). One open interval per registration while it stays on the list —
-- absence from a later pull closes it (valid_to = that list date). icao24 is registry-or-algorithm resolved and
-- may be NULL (a callsign match still suppresses). ReplacingMergeTree(_version) so a close is an insert of the
-- same (registration valid_from) key with valid_to filled and a newer _version — read current state with FINAL.
CREATE TABLE IF NOT EXISTS dim.dim_ladd
(
    registration String,
    callsign     Nullable(String),
    icao24       Nullable(String),
    valid_from   Date,
    valid_to     Nullable(Date),
    _version     DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(_version)
ORDER BY (registration, valid_from);

-- ladd_pulls — processed monthly lists so a same-date re-run is idempotent (the unseen-file gate reads this).
CREATE TABLE IF NOT EXISTS dim.ladd_pulls
(
    list_date   Date,
    object_uri  String,
    loaded_at   DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(loaded_at)
ORDER BY list_date;
