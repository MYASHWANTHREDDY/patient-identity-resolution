{#
  Same VARCHAR-vs-STRING divergence as null_string.sql, but for casting a real expression
  (DATE, INT64) to text rather than a bare NULL literal -- block_keys.sql concatenates
  dob/dob_year/dob_decade into composite block keys and needs this in three places.
#}
{% macro as_string(expression) %}
  {%- if target.type == 'bigquery' -%}
    CAST({{ expression }} AS STRING)
  {%- else -%}
    cast({{ expression }} as varchar)
  {%- endif -%}
{% endmacro %}
