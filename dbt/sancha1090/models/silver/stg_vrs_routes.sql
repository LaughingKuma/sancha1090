{{ config(materialized='table', tags=['reconcile']) }}

-- dim_airports is ref'd only inside the deploy-guard branch below, so parse-time inference (guard
-- false) misses it and the post-deploy build errors; pin the edge explicitly.
-- depends_on: {{ ref('dim_airports') }}

-- Deploy-order guard: dim.dim_vrs_routes is created by clickhouse-init at the operator's deploy, but
-- transform_marts rebuilds from committed code every ~4 min -- emit empty until the table exists.
{%- if execute %}
{%- set vrs_rel = adapter.get_relation(database=none, schema='dim', identifier='dim_vrs_routes') %}
{%- else %}
{%- set vrs_rel = none %}
{%- endif %}

{%- if vrs_rel is not none %}
-- v2 leg grain (Finding 3): explode each schedule into adjacent legs (a 2-hop route is one leg), so a
-- multi-stop tag flight can still vote the leg it flew. Both endpoints must be in dim_airports, origin
-- != dest, and >=1 endpoint in-box (Finding 2 transit gate); leg disambiguation happens at attach.
with parsed as (
    select
        upper(trimBoth(callsign)) as callsign,
        {{ callsign_norm('callsign') }} as callsign_norm,
        splitByChar('-', airport_codes) as hops
    from {{ source('dim', 'dim_vrs_routes') }} final
    where callsign != ''
),
exploded as (
    select
        p.callsign as callsign,
        p.callsign_norm as callsign_norm,
        n as leg_idx,
        p.hops[n] as origin_icao,
        p.hops[n + 1] as dest_icao
    from parsed p
    array join arrayEnumerate(p.hops) as n
    where n < length(p.hops)
),
gated as (
    select e.callsign as callsign, e.callsign_norm as callsign_norm, e.leg_idx as leg_idx,
           e.origin_icao as origin_icao, e.dest_icao as dest_icao
    from exploded e
    join {{ ref('dim_airports') }} oa on oa.icao = e.origin_icao
    join {{ ref('dim_airports') }} da on da.icao = e.dest_icao
    where e.origin_icao != e.dest_icao
      and ({{ in_japan_box('oa.lat', 'oa.lon') }} or {{ in_japan_box('da.lat', 'da.lon') }})
),
-- Route pick AFTER gating (v1's post-gate rn=1 semantics): keep only the alphabetically-first callsign
-- variant that still has a surviving leg, so SFJ43 vs SFJ0043 collapse to one route, legs never mix.
-- Window functions are illegal in WHERE, so the pick is computed in a subquery and filtered outside.
picked as (
    select callsign, callsign_norm, leg_idx, origin_icao, dest_icao
    from (
        select g.callsign as callsign, g.callsign_norm as callsign_norm, g.leg_idx as leg_idx,
               g.origin_icao as origin_icao, g.dest_icao as dest_icao,
               min(g.callsign) over (partition by g.callsign_norm) as pick_callsign
        from gated g
    )
    where callsign = pick_callsign
)
-- Dedup identical (origin, dest) legs within a route (A-B-A-B shuttles), min leg_idx for provenance.
-- n_box_legs (window after GROUP BY) counts the norm's surviving distinct legs; drives the attach pick.
select
    any(callsign) as callsign,
    callsign_norm,
    min(leg_idx) as leg_idx,
    origin_icao,
    dest_icao,
    count() over (partition by callsign_norm) as n_box_legs
from picked
group by callsign_norm, origin_icao, dest_icao
{%- else %}
-- guarded empty until clickhouse-init creates dim.dim_vrs_routes; self-heals on the next build after.
select '' as callsign, '' as callsign_norm, toUInt32(0) as leg_idx,
       '' as origin_icao, '' as dest_icao, toUInt64(0) as n_box_legs where 0
{%- endif %}
