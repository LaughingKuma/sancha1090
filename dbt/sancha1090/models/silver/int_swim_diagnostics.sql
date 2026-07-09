{{ config(materialized='table', tags=['swim']) }}

-- Visibility before SP3b snapping/vote decisions: how often endpoints are non-airport and hexes unresolved.
select
    count(*) as flights,
    countIf(icao24 is not null) as hex_resolved,
    countIf(hex_ambiguous = 1) as hex_ambiguous,
    countIf(dep_point_kind = 'airport') as dep_airport,
    countIf(dep_point_kind is not null and dep_point_kind <> 'airport') as dep_non_airport,
    countIf(dep_point_kind is null) as dep_kind_unknown,   -- else NULL kinds vanish from both buckets (3VL)
    countIf(arr_point_kind = 'airport') as arr_airport,
    countIf(arr_point_kind is not null and arr_point_kind <> 'airport') as arr_non_airport,
    countIf(arr_point_kind is null) as arr_kind_unknown
from {{ ref('int_swim_flight') }}
