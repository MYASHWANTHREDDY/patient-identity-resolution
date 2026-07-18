{#
  Another DuckDB/BigQuery regex divergence, found running Phase 11's real BigQuery build:
  DuckDB's regexp_replace() takes an optional 4th "flags" argument ('g' for global replace);
  BigQuery's REGEXP_REPLACE(value, regex, replacement) only accepts 3 arguments and is
  already global by default, so passing a 4th argument is a signature error there, not a
  no-op. Same dispatch pattern as phonetic_key.sql / parse_vendor_date.sql.
#}
{% macro strip_non_digits(col) %}
  {%- if target.type == 'bigquery' -%}
    REGEXP_REPLACE({{ col }}, '[^0-9]', '')
  {%- else -%}
    regexp_replace({{ col }}, '[^0-9]', '', 'g')
  {%- endif -%}
{% endmacro %}
