{#
  `VARCHAR` is a DuckDB/Postgres type name; BigQuery's equivalent is `STRING` and doesn't
  recognize `VARCHAR` at all ("Type not found: varchar", found running Phase 11's real
  BigQuery build). Used where a column is a structural, always-NULL placeholder (Vendor C
  never sends SSN) and still needs an explicit type for the cast to type-check.
#}
{% macro null_string() %}
  {%- if target.type == 'bigquery' -%}
    CAST(NULL AS STRING)
  {%- else -%}
    cast(null as varchar)
  {%- endif -%}
{% endmacro %}
