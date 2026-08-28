{{ config(
    materialized='table',
    tags=['swim'],
    query_settings={'max_memory_usage': 12000000000},
) }}

-- Physical seam: ClickHouse substitutes CTEs per reference, so as a CTE inside int_swim_flight this
-- full-history GROUP BY over bronze.swim_flightdata ran ~5x per tick (#187); downstream reads a ~4 M-row table.
-- The amendment aggregation itself now lives in the P4 MV (#201) — this reads its ~4 M-row -Merge view instead
-- of re-scanning 80 M bronze rows per tick; the key expression + argMax contract have a single home there.
with latest as (
    select flight_key, latest_tuple
    from {{ source('silver_ch', 'swim_latest') }}
),
keyed as (  -- LID join keys derived once (NULL unless exactly 3 chars) instead of re-deriving per join/select
    select flight_key, latest_tuple,
        if(length(ifNull(latest_tuple.1, '')) = 3, latest_tuple.1, null) as origin_lid,
        if(length(ifNull(latest_tuple.3, '')) = 3, latest_tuple.3, null) as dest_lid
    from latest
),
iata_lookup as (  -- dedupe + non-empty guard are mandatory: raw iata has many '' rows and dup iata values,
    -- so an unguarded join would match ''='' or fan out a flight across duplicate iata rows.
    select iata, any(icao) as icao
    from {{ ref('dim_airports') }}
    where iata != ''
    group by iata
)
select flight_key,
    -- TFMS sometimes files a bare FAA LID (CVG) instead of ICAO; resolve 3-letter codes via
    -- dim_airports.iata (never K-prefix: ANC->PANC), NULL-keyed join so others pass untouched.
    coalesce(ol.icao, latest_tuple.1) as origin_icao,
    latest_tuple.2 as dep_point_kind,
    coalesce(dl.icao, latest_tuple.3) as dest_icao,
    latest_tuple.4 as arr_point_kind,
    latest_tuple.5 as win_start,
    -- kept flight-plan-class messages carry igtd but NO eta, so cap the match window off departure.
    coalesce(latest_tuple.6, latest_tuple.5 + toIntervalHour({{ var('swim_max_flight_hours') }})) as win_end,
    upper(trimBoth(latest_tuple.7)) as callsign
from keyed
left join iata_lookup ol on ol.iata = origin_lid
left join iata_lookup dl on dl.iata = dest_lid
