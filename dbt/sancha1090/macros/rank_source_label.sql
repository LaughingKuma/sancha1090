{# reconcile source_rank -> label; the [1,2,3,4] order is pinned to the assert_flight_opinions_rank_map
   singular test (swim=1, opensky_flights=2, adsblol=3, opensky_states=4). Used at the origin/dest win sites. #}
{% macro rank_source_label(rank_col) -%}
transform({{ rank_col }}, [1, 2, 3, 4], ['swim', 'opensky_flights', 'adsblol', 'opensky_states'], 'opensky_states')
{%- endmacro %}
