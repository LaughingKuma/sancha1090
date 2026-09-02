{# One row per UTC day a [start, end] window touches (#190 recipe): bounds an interval-overlap
   join to same-day pairs, since overlapping windows always share at least one day. #}
{# least/greatest guards an inverted window — an empty range() would arrayJoin the row away, silently
   dropping it from the join's LEFT side. Callers keep a not-null guard on both window columns. #}
{% macro overlap_days(start_col, end_col) -%}
arrayJoin(range(
        toUInt32(least(toRelativeDayNum({{ start_col }}), toRelativeDayNum({{ end_col }}))),
        toUInt32(greatest(toRelativeDayNum({{ start_col }}), toRelativeDayNum({{ end_col }}))) + 1
    ))
{%- endmacro %}
