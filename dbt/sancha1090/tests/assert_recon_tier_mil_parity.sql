-- Full military parity: is_military must equal membership in (rooftop dbFlags ∪ latest ADSBx mil) —
-- a dropped union arm or join regression fails in either direction.

-- adsbx arm gated like the model's: optional pre-deploy DDL on upgraded hosts (one-shot init)
{%- if execute %}
{%- set adsbx_rel = adapter.get_relation(database=none, schema='bronze', identifier='adsbx_aircraft_db') %}
{%- else %}
{%- set adsbx_rel = none %}
{%- endif %}

with mil as (
    -- literal names, not source(): an edge into the adsb closure would eager-select this test into
    -- transform_adsb_silver's +tag:adsb run, where the tier mart it gates is not built
    select distinct lower(hex) as icao24
    from bronze.adsb_states
    where bitAnd(coalesce(db_flags, 0), 1) != 0 and hex is not null
    {%- if adsbx_rel is not none %}
    union distinct
    select distinct lower(icao24) as icao24
    from bronze.adsbx_aircraft_db
    where mil = 1 and icao24 is not null
      and as_of_date = (select max(as_of_date) from bronze.adsbx_aircraft_db)
    {%- endif %}
)
-- either arm can gain a first-time military hex between mart build and this test (hourly rooftop
-- appends, weekly ADSBx snapshots) — a one-tick red that self-heals on the next rebuild
select t.flight_id, t.icao24, t.is_military
from {{ ref('fct_flight_recon_tier') }} t
where t.is_military != toUInt8(t.icao24 in (select icao24 from mil))
