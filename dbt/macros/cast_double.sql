{#
  Found casting lab_results_normalized's string-typed result_value to a numeric type: DuckDB
  has no `FLOAT64` (BigQuery's double-precision type name) -- it fails outright ("Did you mean
  'float4'?"). DuckDB's own `FLOAT` is single-precision (a cast through it silently loses
  precision -- '123.45' comes back as 123.44999694824219), and `DOUBLE` is its real
  double-precision type. BigQuery accepts `FLOAT64` as the type name; it does not recognize
  `DOUBLE`. Two names for the same IEEE 754 double, resolved per target, same dispatch pattern
  as phonetic_key.sql / parse_vendor_date.sql / strip_non_digits.sql.
#}
{% macro cast_double(expr) %}
  {%- if target.type == 'bigquery' -%}
    CAST({{ expr }} AS FLOAT64)
  {%- else -%}
    CAST({{ expr }} AS DOUBLE)
  {%- endif -%}
{% endmacro %}
