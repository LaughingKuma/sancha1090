{# Shared states-staging shape (stg_states / stg_states_adsblol): typed rename of the bronze columns,
   latest-ingested_at dedup per (icao24, snapshot_time), then re-projection without rn. #}
{% macro states_staging_body(src_relation, where_sql, extra_cols=[]) -%}
with src as (
    select *
    from {{ src_relation }}
    where {{ where_sql }}
),
typed as (
    select
        icao24,
        nullIf(trimBoth(callsign), '')  as callsign,
        origin_country,
        time_position,
        last_contact,
        longitude                       as longitude,
        latitude                        as latitude,
        baro_altitude                   as baro_altitude_m,
        on_ground                       as on_ground,
        velocity                        as velocity_mps,
        true_track                      as track_deg,
        vertical_rate                   as vertical_rate_mps,
        geo_altitude                    as geo_altitude_m,
        squawk,
        spi                             as spi,
        position_source                 as position_source,
        snapshot_time,
        region,
        ingested_at{%- for c in extra_cols %},
        {{ c }}{%- endfor %}
    from src
),
dedup as (
    select
        typed.*,
        {# ingested_at alone is not a total order — tied timestamps with divergent content would make
           rebuild output arbitrary; the full content tail pins one winner (zero ambiguous groups live, 2026-08-11) #}
        row_number() over (
            partition by icao24, snapshot_time
            order by ingested_at desc, callsign, origin_country, time_position, last_contact,
                     longitude, latitude, baro_altitude_m, on_ground, velocity_mps, track_deg,
                     vertical_rate_mps, geo_altitude_m, squawk, spi, position_source, region
                     {%- for c in extra_cols %}, {{ c }}{%- endfor %}
        ) as rn
    from typed
)
select
    icao24, callsign, origin_country, time_position, last_contact,
    longitude, latitude, baro_altitude_m, on_ground, velocity_mps,
    track_deg, vertical_rate_mps, geo_altitude_m, squawk, spi,
    position_source, snapshot_time, region, ingested_at{%- for c in extra_cols %}, {{ c }}{%- endfor %}
from dedup
where rn = 1
{%- endmacro %}
