{{ config(materialized='table', tags=['swim']) }}

-- Rankless per-source opinion projection (source_rank is stamped at the SP3b int_flight_opinions UNION, like
-- every other source model). Vote-eligible only: a resolved non-ambiguous hex AND at least one usable endpoint.
select
    'swim' as source,
    icao24, win_start, win_end, callsign, origin_icao, dest_icao
from {{ ref('int_swim_flight') }}
where icao24 is not null
  and hex_ambiguous = 0
  and (origin_icao is not null or dest_icao is not null)
