-- Pins the OpenSky unit conversions (the spec's #1 pre-listed trap): baro metres->ft (x3.28084) and
-- velocity m/s->kt (x1.94384). Dropping either reads ~3.28x / ~1.94x low. quantileExact, not median() --
-- median() is approximate (reservoir-sampled) and non-reproducible, and this test gates every transform tick.
-- Measured after the daily-partition rebuild (source='opensky', on_ground=0, n=2.01M): exact median
-- alt_ft = 29,900 (metres-kept ~9,113); gs_kt = 420.7 (m/s-kept ~216.5). Floors retain >=30%
-- separation (alt 18,000: +98% over dropped / -40% under correct; gs 288: +33% / -32%).
-- Airborne-only so ground rows (alt 0) don't drag the medians.
select med_alt_ft, med_gs_kt
from (
    select quantileExact(0.5)(alt_ft) as med_alt_ft, quantileExact(0.5)(gs_kt) as med_gs_kt
    from {{ ref('fct_flight_path') }}
    where source = 'opensky' and on_ground = 0
)
where med_alt_ft <= 18000 or med_gs_kt <= 288
