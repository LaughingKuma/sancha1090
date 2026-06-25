{# ClickHouse-dialect helpers. #}

{# hex (ICAO 24-bit) -> registration country via the range_hashed dict that replaces the dim_hex_country
   range LEFT JOIN: parse the hex big-endian to a UInt32, then dictGetOrNull with the synthetic group_id=0
   the dict's PRIMARY KEY needs. coalesce(hex,'') keeps the dict RANGE arg a non-nullable UInt32 (CH rejects
   Nullable, and if() evaluates both branches). The match('^[0-9a-f]{1,6}$') guard rejects readsb '~'-prefixed
   non-ICAO addresses while accepting leading-zero-trimmed registry ICAO24s like '6279'/'0926e' —
   leftPad(...,8,'0') zero-extends them so the dict key resolves exactly. #}
{% macro ch_hex_country(hex_expr) -%}
{%- set h = "lower(coalesce(" ~ hex_expr ~ ", ''))" -%}
if(
    match({{ h }}, '^[0-9a-f]{1,6}$'),
    dictGetOrNull(
        'dim.dict_hex_country', 'country', toUInt8(0),
        reinterpretAsUInt32(reverse(unhex(leftPad({{ h }}, 8, '0'))))
    ),
    NULL
)
{%- endmacro %}

{# dbt-clickhouse loads a blank seed CSV cell as '' (some warehouses coerce blanks to NULL). Coerce blank ->
   NULL so seed-sourced columns (e.g. dim_airports.iata/city, blank for ~1528/51 airports) read NULL instead
   of an empty string. #}
{% macro ch_blank_null(col) -%}
nullIf({{ col }}, '')
{%- endmacro %}
