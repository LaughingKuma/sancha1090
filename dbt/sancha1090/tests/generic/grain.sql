{# Shared grain pin: one row per declared column list; replaces the same-shape singular asserts. #}
{% test grain(model, column_names) %}
select {{ column_names | join(', ') }}
from {{ model }}
group by {{ column_names | join(', ') }}
having count() > 1
{% endtest %}
