-- Hex-country range lookup reconciled in the spike: hex BETWEEN block_lo/hi is a non-equi
-- join CH can't stream, so it becomes a RANGE_HASHED dictionary + dictGet (99.997% resolved
-- = Trino, 81 ms / 65 MiB over 19.2M rows vs ~5.7 GiB naive). Source = dim.dim_hex_country
-- (empty now; the dict lazy-loads and refreshes on LIFETIME once P3 seeds it).
--
-- RANGE_HASHED needs a PRIMARY KEY distinct from its RANGE bounds, but the hex ranges have no
-- natural grouping id (block_lo can't be both the key AND the range MIN — CH rejects the
-- doubled column). A single synthetic group_id=0 over the globally non-overlapping ranges is
-- the idiomatic fix, so the lookup contract gains a constant id arg:
--   dictGet('dim.dict_hex_country', 'country', toUInt8(0),
--           reinterpretAsUInt32(reverse(unhex(leftPad(hex, 8, '0')))))
-- guarded by match(hex, '^[0-9a-f]{6}$') so readsb '~'-prefixed non-ICAO addresses are
-- filtered before any dictGet (an out-of-range probe returns '' rather than throwing).
CREATE DICTIONARY IF NOT EXISTS dim.dict_hex_country
(
    group_id UInt8,
    block_lo UInt32,
    block_hi UInt32,
    country  String
)
PRIMARY KEY group_id
SOURCE(CLICKHOUSE(QUERY 'SELECT 0 AS group_id, block_lo, block_hi, country FROM dim.dim_hex_country'))
LAYOUT(RANGE_HASHED())
RANGE(MIN block_lo MAX block_hi)
LIFETIME(MIN 600 MAX 900);

-- Dictionaries lazy-load by default, so a malformed dict would otherwise only surface on the
-- first dictGet in P3. Force the (empty) load now so provisioning fails loud if the structure
-- is wrong and the dict reports RangeHashed / LOADED immediately.
SYSTEM RELOAD DICTIONARY dim.dict_hex_country;
