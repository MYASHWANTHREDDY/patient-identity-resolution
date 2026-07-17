{#
  Layer datasets (raw_standard, conformance, matching, serving, quality) are literal
  schema/dataset names on both targets -- not prefixed with the target's default schema.
  This keeps `conformance.patient_normalized` addressable the same way whether the target
  is local DuckDB or BigQuery (PROJECT_CONSTITUTION.md #9).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none -%}
    {{ target.schema }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
