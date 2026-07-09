{{ config(materialized='table', tags=['reconcile']) }}

-- Jet-airliner airframes for the SP4 runway feasibility gate. ICAO designator (L#J) is authoritative
-- where the registry has one; body_class fallback covers designator-less frames (C130/C30J excluded:
-- typed 'quad' but L4T turboprops that legitimately use short strips). Unknown type -> not a jet (fail open).
with frames as (
    select
        lower(coalesce(r.icao24, a.icao24)) as icao24,
        coalesce(nullIf(r.icaoaircrafttype, ''), '') as designator,
        coalesce(nullIf(r.typecode, ''), nullIf(a.typecode, ''), '') as typecode
    from {{ ref('dim_aircraft_registry') }} r
    full outer join {{ ref('dim_aircraft') }} a on a.icao24 = lower(r.icao24)
)
select f.icao24 as icao24
from frames f
left join {{ ref('dim_aircraft_types') }} t on t.typecode = f.typecode
where if(f.designator != '',
         match(f.designator, '^L[0-9]J$'),
         coalesce(t.body_class, '') in ('narrowbody', 'widebody', 'quad')
             and f.typecode not in ('C130', 'C30J'))
