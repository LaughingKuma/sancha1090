{{ config(tags=['reconcile']) }}
-- depends_on: {{ source('dim', 'dim_vrs_routes') }}
-- depends_on: {{ ref('stg_vrs_routes') }}
-- depends_on: {{ ref('int_flight_attach') }}
-- depends_on: {{ ref('int_flight_spine') }}

{%- if execute %}
{%- set vrs_rel = adapter.get_relation(database=none, schema='dim', identifier='dim_vrs_routes') %}
{%- else %}
{%- set vrs_rel = none %}
{%- endif %}

{%- if vrs_rel is not none %}
-- Finding 3: first prove every staged row is an adjacent pair in the raw schedule, independently of
-- the staging implementation. Then prove each fully jet-gate-surviving vote is one of those staged legs.
-- Together these prevent a first->last shortcut from passing merely because both staging and attach agree.
with raw_routes as (
    select upper(trimBoth(callsign)) as callsign, splitByChar('-', airport_codes) as hops
    from {{ source('dim', 'dim_vrs_routes') }} final
    where callsign != ''
),
raw_legs as (
    select callsign, n as leg_idx, hops[n] as origin_icao, hops[n + 1] as dest_icao
    from raw_routes
    array join arrayEnumerate(hops) as n
    where n < length(hops)
),
invalid_staged as (
    select 'staged_not_adjacent' as issue, cast(NULL as Nullable(UInt64)) as flight_id,
           v.callsign_norm as callsign_norm, v.origin_icao as origin_icao, v.dest_icao as dest_icao
    from {{ ref('stg_vrs_routes') }} v
    where (v.callsign, v.leg_idx, v.origin_icao, v.dest_icao) not in (
        select callsign, leg_idx, origin_icao, dest_icao from raw_legs
    )
),
invalid_votes as (
    select 'vote_not_staged_leg' as issue, fa.flight_id as flight_id,
           {{ callsign_norm('sp.anchor_callsign') }} as callsign_norm,
           fa.origin_icao as origin_icao, fa.dest_icao as dest_icao
    from {{ ref('int_flight_attach') }} fa
    join {{ ref('int_flight_spine') }} sp on sp.flight_id = fa.flight_id
    left join {{ ref('stg_vrs_routes') }} v
           on v.callsign_norm = {{ callsign_norm('sp.anchor_callsign') }}
          and v.origin_icao = fa.origin_icao
          and v.dest_icao = fa.dest_icao
    where fa.source = 'vrs_routes'
      and fa.origin_icao is not null
      and fa.dest_icao is not null
      and v.callsign_norm is null
)
select issue, flight_id, callsign_norm, origin_icao, dest_icao from invalid_staged
union all
select issue, flight_id, callsign_norm, origin_icao, dest_icao from invalid_votes
{%- else %}
select '' as issue, cast(NULL as Nullable(UInt64)) as flight_id, '' as callsign_norm,
       '' as origin_icao, '' as dest_icao
where 0
{%- endif %}
