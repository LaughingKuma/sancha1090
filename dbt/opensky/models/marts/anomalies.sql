select
    icao24,
    callsign,
    origin_country,
    snapshot_time,
    latitude,
    longitude,
    baro_altitude_m,
    velocity_mps,
    case
        when baro_altitude_m > 15000           then 'altitude_too_high'
        when baro_altitude_m < -500            then 'altitude_below_sea_level'
        when velocity_mps > 350                then 'velocity_too_high'
        when velocity_mps < 0                  then 'negative_velocity'
        when latitude not between -90 and 90   then 'invalid_latitude'
        when longitude not between -180 and 180 then 'invalid_longitude'
    end as anomaly_type
from {{ ref('stg_states') }}
where
    baro_altitude_m > 15000
    or baro_altitude_m < -500
    or velocity_mps > 350
    or velocity_mps < 0
    or latitude not between -90 and 90
    or longitude not between -180 and 180
