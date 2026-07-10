{{ config(materialized='table', tags=['swim']) }}

-- Rankless per-source opinion projection (source_rank is stamped at the int_flight_opinions UNION, like
-- every other source model). Vote-eligible only: a resolved non-ambiguous hex AND at least one usable endpoint.
select
    'swim' as source,
    f.icao24 as icao24, f.win_start as win_start, f.win_end as win_end,
    f.callsign as callsign, f.origin_icao as origin_icao, f.dest_icao as dest_icao
from {{ ref('int_swim_flight') }} f
left join {{ ref('dim_airports') }} oa on oa.icao = f.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = f.dest_icao
where f.icao24 is not null
  and f.hex_ambiguous = 0
  and (f.origin_icao is not null or f.dest_icao is not null)
  -- Finding 2 scope gate: a filed O/D with no in-box endpoint is observable only as a transit --
  -- unfalsifiable by any observed voter (join_use_nulls: unknown airports fail closed via coalesce).
  and (coalesce({{ in_japan_box('oa.lat', 'oa.lon') }}, false)
       or coalesce({{ in_japan_box('da.lat', 'da.lon') }}, false))
