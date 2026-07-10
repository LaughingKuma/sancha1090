{# Observation-box membership. NULL lat/lon propagates NULL -- callers that LEFT JOIN coords
   coalesce(..., false) so unknown airports fail closed. #}
{% macro in_japan_box(lat_expr, lon_expr) -%}
(({{ lat_expr }}) between {{ var('japan_box_lamin') }} and {{ var('japan_box_lamax') }}
 and ({{ lon_expr }}) between {{ var('japan_box_lomin') }} and {{ var('japan_box_lomax') }})
{%- endmacro %}
