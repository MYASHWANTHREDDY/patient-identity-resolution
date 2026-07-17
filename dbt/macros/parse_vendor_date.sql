{#
  Parses a Layer-1 STRING date in a vendor's native format into a DATE, or NULL on
  failure -- never an error. A malformed date is a data quality finding to report on, not a
  pipeline crash (PROJECT_CONSTITUTION.md #5, Layer 1 -> Layer 2 contract).
  `strptime_format` uses strftime-style directives (%Y-%m-%d, %m/%d/%Y, %d-%b-%Y), which are
  compatible between DuckDB's strptime and BigQuery's PARSE_DATE.
#}
{% macro parse_vendor_date(col, strptime_format) %}
  {%- if target.type == 'bigquery' -%}
    SAFE.PARSE_DATE('{{ strptime_format }}', {{ col }})
  {%- else -%}
    CAST(try_strptime({{ col }}, '{{ strptime_format }}') AS DATE)
  {%- endif -%}
{% endmacro %}
