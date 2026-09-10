{# SP4 feasibility gate: a jet airliner cannot use a known-short runway; an unknown-runway (0)
   small_airport is gated too -- the top offenders (JGSDF strips) carry no runway data. NULL-safe:
   an airport missing from dim_airports fails OPEN (coalesce -> 0/'' -> both arms false). #}
{% macro jet_infeasible_airport(runway_col, type_col) -%}
((coalesce({{ runway_col }}, 0) > 0 and coalesce({{ runway_col }}, 0) < {{ var('jet_min_runway_ft') }})
 or (coalesce({{ type_col }}, '') = 'small_airport' and coalesce({{ runway_col }}, 0) = 0))
{%- endmacro %}

{# SP4 single source of truth for the feasibility gate: an airline-shaped jet endpoint at an
   infeasible airport. airline_expr/jet_expr are already-rendered booleans (a column, a macro call,
   or a coalesced flag); runway_col/type_col feed jet_infeasible_airport. #}
{% macro jet_infeasible_endpoint(airline_expr, jet_expr, runway_col, type_col) -%}
(({{ airline_expr }}) and ({{ jet_expr }}) and {{ jet_infeasible_airport(runway_col, type_col) }})
{%- endmacro %}

{# #213A: iata != '' and scheduled_service both let a legacy-labeled military field (e.g. Kadena)
   wrongly outrank a closer real airfield -- airport_type is the one signal that doesn't. #}
{% macro real_airfield(type_col) -%}
({{ type_col }} not in ('heliport', 'seaplane_base'))
{%- endmacro %}

{# One snap tier for every lane (#214): a real airfield within snap_iata_pref_km beats a nearer heliport,
   then distance, then a.icao -- distance alone is not a total order (ATUA/AYUA share coordinates). #}
{% macro snap_order(fix_lat, fix_lon) -%}
if({{ real_airfield('a.airport_type') }} and {{ haversine_km(fix_lat, fix_lon, 'a.lat', 'a.lon') }} <= {{ var('snap_iata_pref_km') }}, 0, 1),
{{ haversine_km(fix_lat, fix_lon, 'a.lat', 'a.lon') }}, a.icao
{%- endmacro %}

{# Callsign match key for the vrs_routes lane: transmitted callsigns zero-pad the flight number
   (SFJ0043) where the schedule DB doesn't (SFJ43); normalize both sides with the same expression. #}
{% macro callsign_norm(callsign_col) -%}
replaceRegexpOne(upper(trimBoth({{ callsign_col }})), '^([A-Z]{3})0+([0-9])', '\\1\\2')
{%- endmacro %}
