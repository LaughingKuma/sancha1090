-- dim_hex_country backs the range_hashed dictionary in 03_dict_hex_country.sql. Empty here; loaded in P3.

CREATE DATABASE IF NOT EXISTS dim;

-- dim_hex_country.csv: block_lo,block_hi,country (214 ranges + header)
CREATE TABLE IF NOT EXISTS dim.dim_hex_country
(block_lo UInt32, block_hi UInt32, country String)
ENGINE = MergeTree ORDER BY block_lo;
