{#
  DuckDB's array aggregate is `list(...)`; BigQuery's is `ARRAY_AGG(...)`. Same dispatch
  pattern as phonetic_key.sql / parse_vendor_date.sql -- one dialect divergence, absorbed
  by a macro rather than duplicating the model per target (PROJECT_CONSTITUTION.md #10).
#}
{% macro array_agg_distinct(expression) %}
  {%- if target.type == 'bigquery' -%}
    ARRAY_AGG(DISTINCT {{ expression }} IGNORE NULLS)
  {%- else -%}
    list(distinct {{ expression }})
  {%- endif -%}
{% endmacro %}
