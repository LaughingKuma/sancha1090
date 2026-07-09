{# reconcile source_rank -> label; the [1..5] order is pinned to the assert_flight_opinions_rank_map
   singular test (swim=1, vrs_routes=2, opensky_flights=3, adsblol=4, opensky_states=5). #}
{% macro rank_source_label(rank_col) -%}
transform({{ rank_col }}, [1, 2, 3, 4, 5], ['swim', 'vrs_routes', 'opensky_flights', 'adsblol', 'opensky_states'], 'opensky_states')
{%- endmacro %}
