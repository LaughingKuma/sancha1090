-- Summary bounds: fraction in [0,1], non-negative gap, per-source counts sum to the total, ordered fixes.
select flight_id
from {{ ref('fct_flight_path_summary') }}
where observed_fraction < 0 or observed_fraction > 1
   or largest_gap_s < 0
   or n_points != n_adsb + n_adsblol + n_opensky
   or first_fix_ts > last_fix_ts
