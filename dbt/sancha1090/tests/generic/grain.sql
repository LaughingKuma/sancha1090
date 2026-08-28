{# Shared grain pin: one row per declared column list; replaces the same-shape singular asserts. #}
{# ordered=true walks the table in key order instead of hashing every key — cheap ONLY while column_names is a prefix of the model's physical sorting key (tests/test_dbt_grain_ordered.py pins it); otherwise it degrades to a full sort. #}
{% test grain(model, column_names, ordered=false) %}
{% if ordered %}
with adjacent as (
    select
        {{ column_names | join(', ') }},
        lagInFrame(tuple(toUInt8(1), {{ column_names | join(', ') }}), 1) over (
            order by {{ column_names | join(', ') }}
            rows between 1 preceding and current row
        ) as prev_key
    from {{ model }}
)
select {{ column_names | join(', ') }}
from adjacent
-- tuple = propagates NULL, which would let a NULL-keyed duplicate pass; is not distinct from does not.
where tuple(toUInt8(1), {{ column_names | join(', ') }}) is not distinct from prev_key
{% else %}
select {{ column_names | join(', ') }}
from {{ model }}
group by {{ column_names | join(', ') }}
having count() > 1
{% endif %}
{% endtest %}
