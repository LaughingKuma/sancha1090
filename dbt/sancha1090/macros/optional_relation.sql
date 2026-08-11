{# Deploy-order-guard idiom: optional warehouse objects (one-shot clickhouse-init / manual DDL) resolve
   to none until they exist; execute-gated so parse never touches the warehouse. #}
{% macro optional_relation(schema, identifier) %}
{%- if execute %}
{{- return(adapter.get_relation(database=none, schema=schema, identifier=identifier)) }}
{%- else %}
{{- return(none) }}
{%- endif %}
{% endmacro %}
