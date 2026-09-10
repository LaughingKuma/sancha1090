{# A nullable default keeps an airframe's first fix from matching the column type's zero. #}
{% macro prev_fix(col) -%}
lagInFrame(toNullable({{ col }}), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row)
{%- endmacro %}
