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
-- Two-airport routes only (v1): multi-stop hop lists (~1%, cargo) need position disambiguation we
-- deliberately skip. Both endpoints must exist in dim_airports (same rule as curated overrides).
-- Dedup on callsign_norm: SFJ43 and SFJ0043 both in the dim must not become two votes.
with parsed as (
    select
        upper(trimBoth(callsign)) as callsign,
        {{ callsign_norm('callsign') }} as callsign_norm,
        splitByChar('-', airport_codes) as hops
    from {{ source('dim', 'dim_vrs_routes') }} final
    where callsign != ''
)
select callsign, callsign_norm, origin_icao, dest_icao
from (
    select p.callsign as callsign, p.callsign_norm as callsign_norm,
           p.hops[1] as origin_icao, p.hops[2] as dest_icao,
           row_number() over (partition by p.callsign_norm order by p.callsign) as rn
    from parsed p
    join {{ ref('dim_airports') }} oa on oa.icao = p.hops[1]
    join {{ ref('dim_airports') }} da on da.icao = p.hops[2]
    where length(p.hops) = 2
) where rn = 1
{%- else %}
-- guarded empty until clickhouse-init creates dim.dim_vrs_routes; self-heals on the next build after.
select '' as callsign, '' as callsign_norm, '' as origin_icao, '' as dest_icao where 0
{%- endif %}
