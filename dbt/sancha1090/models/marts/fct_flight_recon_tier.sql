{{ config(materialized='table') }}

-- ADSBx is optional pre-deploy on upgraded hosts (one-shot clickhouse-init) — same guard as
-- dim_aircraft_registry; absent ⇒ rooftop-only flags, honoring the yml's is_military=0 promise.
{%- set adsbx_rel = optional_relation('bronze', 'adsbx_aircraft_db') %}

with head as (
    -- mirrors pathfusion.HEAD_QUERY (epoch fallback on an empty mart) so the mart and serve-time
    -- fusion agree on the settled/provisional seam
    select coalesce(max(day_key), toDate('1970-01-01')) as head_day
    from {{ ref('fct_flight_path') }}
),
-- Military = rooftop dbFlags ∪ latest ADSBx mil: each arm catches airframes the other misses
-- (183 vs 6 on reconciled flights, 2026-07-31).
mil as (
    select distinct lower(hex) as icao24
    from {{ source('bronze', 'adsb_states') }}
    where bitAnd(coalesce(db_flags, 0), 1) != 0 and hex is not null
    {%- if adsbx_rel is not none %}
    union distinct
    select distinct lower(icao24) as icao24
    from {{ source('bronze', 'adsbx_aircraft_db') }}
    where mil = 1 and icao24 is not null
      and as_of_date = (select max(as_of_date) from {{ source('bronze', 'adsbx_aircraft_db') }})
    {%- endif %}
)
select
    r.flight_id                                   as flight_id,
    lower(r.icao24)                               as icao24,
    r.callsign                                    as callsign,
    toDate(r.start_time)                          as start_day,
    -- Window-edge gaps count too: largest_gap_s is inter-fix only (0 for a one-point path), so fixes
    -- clustered at one end of the window must not read as settled. NULL when the flight has no path.
    if(s.flight_id is not null,
       greatest(
           coalesce(s.largest_gap_s, 0),
           if(s.first_fix_ts > r.start_time, dateDiff('second', r.start_time, s.first_fix_ts), 0),
           if(s.last_fix_ts < r.end_time, dateDiff('second', s.last_fix_ts, r.end_time), 0)),
       NULL)                                      as effective_gap_s,
    multiIf(
        s.flight_id is not null and effective_gap_s <= {{ var('recon_tier_gap_s') }}, 'settled',
        s.flight_id is not null, 'estimated',
        r.start_time is not null and toDate(r.start_time) > (select head_day from head), 'provisional',
        'none')                                   as tier,
    s.n_points                                    as n_points,
    s.largest_gap_s                               as largest_gap_s,
    s.observed_fraction                           as observed_fraction,
    toUInt8(m.icao24 is not null)                 as is_military
from {{ ref('fct_flights_reconciled') }} r
left join {{ ref('fct_flight_path_summary') }} s on s.flight_id = r.flight_id
left join mil m on m.icao24 = lower(r.icao24)
where r.flight_id is not null
