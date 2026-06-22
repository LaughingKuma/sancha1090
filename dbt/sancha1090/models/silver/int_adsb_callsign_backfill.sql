{{ config(materialized='table', tags=['adsb']) }}

-- ADS-B identity messages broadcast ~10x less often than position, so ~4% of frames land with a decoded
-- position but a blank callsign (worse at the range edge, where the rarer ID frame fails CRC). The same
-- airframe is in the OpenSky context feed within seconds; take the nearest OpenSky callsign inside the
-- backfill window. row_number()=1 keeps this single-valued per (hex, capture_ts) so the LEFT join into
-- fct_adsb_state stays row-count-preserving.
{% if target.type == 'clickhouse' %}
-- CH: a range-join + row_number nearest-abs is exact but the heaviest op in the lane (5.7 GiB in the
-- spike). A TWO-SIDED ASOF is exact AND cheap: one ASOF picks the nearest *preceding* OpenSky snapshot,
-- one the nearest *following*; row_number()=1 over abs(d) then picks the closer of the two — the true
-- nearest is necessarily one of them. (A single preceding-only ASOF under-fills by 18%, so both sides
-- are required for parity.) snapshot_time is DateTime64(6) → micro-epoch seconds to match to_unixtime.
with miss as (
    select distinct hex, capture_ts
    from {{ source('bronze', 'adsb_states') }}
    where flight is null or trimBoth(flight) = ''
),
opensky as (
    select icao24, toUnixTimestamp64Micro(snapshot_time) / 1e6 as snap_epoch, trimBoth(callsign) as callsign
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trimBoth(callsign) <> ''
),
preceding as (
    select m.hex, m.capture_ts, o.callsign, o.snap_epoch, (m.capture_ts - o.snap_epoch) as d
    from miss m
    asof left join opensky o
      on o.icao24 = m.hex and o.snap_epoch <= m.capture_ts
    where o.callsign is not null and (m.capture_ts - o.snap_epoch) <= {{ var('callsign_backfill_window_s') }}
),
following as (
    select m.hex, m.capture_ts, o.callsign, o.snap_epoch, (o.snap_epoch - m.capture_ts) as d
    from miss m
    asof left join opensky o
      on o.icao24 = m.hex and o.snap_epoch >= m.capture_ts
    where o.callsign is not null and (o.snap_epoch - m.capture_ts) <= {{ var('callsign_backfill_window_s') }}
),
nearest as (
    select
        hex, capture_ts, callsign,
        row_number() over (
            partition by hex, capture_ts
            -- nearest wins; later snapshot then callsign break ties (matches the Trino determinism order).
            order by abs(d) asc, snap_epoch desc, callsign asc
        ) as rn
    from (select * from preceding union all select * from following)
)
select hex, capture_ts, callsign as filled_callsign
from nearest
where rn = 1
{% else %}
with miss as (
    select distinct hex, capture_ts
    from {{ source('bronze', 'adsb_states') }}
    where flight is null or trim(flight) = ''
),
opensky as (
    select icao24, to_unixtime(snapshot_time) as snap_epoch, trim(callsign) as callsign
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trim(callsign) <> ''
),
nearest as (
    select
        m.hex,
        m.capture_ts,
        o.callsign,
        row_number() over (
            partition by m.hex, m.capture_ts
            -- nearest snapshot wins; later snapshot then callsign break ties (bronze isn't deduped) for determinism.
            order by abs(o.snap_epoch - m.capture_ts), o.snap_epoch desc, o.callsign asc
        ) as rn
    from miss m
    join opensky o
      on o.icao24 = m.hex
     and o.snap_epoch between m.capture_ts - {{ var('callsign_backfill_window_s') }}
                         and m.capture_ts + {{ var('callsign_backfill_window_s') }}
)
select hex, capture_ts, callsign as filled_callsign
from nearest
where rn = 1
{% endif %}
