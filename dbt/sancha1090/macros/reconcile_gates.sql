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

{# Callsign match key for the vrs_routes lane: transmitted callsigns zero-pad the flight number
   (SFJ0043) where the schedule DB doesn't (SFJ43); normalize both sides with the same expression. #}
{% macro callsign_norm(callsign_col) -%}
replaceRegexpOne(upper(trimBoth({{ callsign_col }})), '^([A-Z]{3})0+([0-9])', '\\1\\2')
{%- endmacro %}
