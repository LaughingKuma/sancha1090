{{ config(tags=['reconcile']) }}
-- #213 D: an adsb.lol anchor that any lane saw in the box inside its window must reach the mart, one arm per
-- lane so a lane the model stopped reading cannot hide behind another admitting the same flights.

-- Trailing 3 days by flight_start so the bronze probes stay a PK range, not a history scan.
{%- set since = "toStartOfDay(now('UTC')) - interval 3 day" %}
-- bronze.adsblol_states ends 2026-07-02, past any trailing window: that arm also probes the June coverage
-- holes the lane was added for, or it never reads the table it is meant to prove.
{%- set hist_from = "toDateTime64('2026-06-20 00:00:00', 6, 'UTC')" %}
{%- set hist_to = "toDateTime64('2026-07-02 00:00:00', 6, 'UTC')" %}
with sp as (
    select flight_id, icao24, flight_start, flight_end,
           {{ overlap_days('flight_start', 'flight_end') }} as overlap_day
    from {{ ref('int_flight_spine') }}
    where anchor_source = 'adsblol' and flight_end is not null
      and (flight_start >= {{ since }} or flight_start between {{ hist_from }} and {{ hist_to }})
      and flight_id not in (select flight_id from {{ ref('fct_flights_reconciled') }})
)
select distinct 'opensky_states' as lane, sp.flight_id, sp.icao24, sp.flight_start
from {{ source('bronze', 'opensky_states') }} f
join sp on sp.icao24 = f.icao24 and sp.overlap_day = toUInt32(toRelativeDayNum(f.snapshot_time))
where f.snapshot_time >= {{ since }} and {{ in_japan_box('f.latitude', 'f.longitude') }}
  and f.snapshot_time between sp.flight_start and sp.flight_end
union all
select distinct 'adsblol_states' as lane, sp.flight_id, sp.icao24, sp.flight_start
from {{ source('bronze', 'adsblol_states') }} f
join sp on sp.icao24 = f.icao24 and sp.overlap_day = toUInt32(toRelativeDayNum(f.snapshot_time))
where (f.snapshot_time >= {{ since }} or f.snapshot_time between {{ hist_from }} and {{ hist_to }})
  and {{ in_japan_box('f.latitude', 'f.longitude') }}
  and f.snapshot_time between sp.flight_start and sp.flight_end
union all
select distinct 'adsb_states' as lane, sp.flight_id, sp.icao24, sp.flight_start
from {{ source('bronze', 'adsb_states') }} f
join sp on sp.icao24 = lower(f.hex)
       and sp.overlap_day = toUInt32(toRelativeDayNum(toDateTime64(f.capture_ts, 6, 'UTC')))
where f.capture_ts >= toUnixTimestamp({{ since }}) and {{ in_japan_box('f.lat', 'f.lon') }}
  and toDateTime64(f.capture_ts, 6, 'UTC') between sp.flight_start and sp.flight_end
