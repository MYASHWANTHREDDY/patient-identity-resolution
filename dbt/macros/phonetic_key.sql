{#
  BigQuery ships SOUNDEX natively. DuckDB (as of dbt-duckdb 1.10 / duckdb 1.5) does not, so
  the `else` branch is a hand-rolled equivalent: map letters to Soundex digit classes, collapse
  adjacent duplicate codes, drop the first letter's own code, strip vowel-class zeros, and
  pad/truncate to 3 digits. Verified to match the transposition case blocking exists to survive:
  soundex('SMITH') = soundex('SMTIH') = 'S530' on both branches.

  Adjacent-duplicate collapse deliberately avoids regex backreferences (`(.)\1+`) -- DuckDB's
  regex engine errors on that pattern once it's nested inside a CREATE TABLE AS this deep
  (reproduces outside dbt too; not chasing the internal cause). Plain literal `replace()`
  calls, one per digit class, three passes deep (collapses runs up to length 8 -- names never
  produce runs anywhere near that long), sidestep it entirely.
#}
{% macro phonetic_key(col) %}
  {%- if target.type == 'bigquery' -%}
    SOUNDEX({{ col }})
  {%- else -%}
    {%- set cleaned = "regexp_replace(upper(" ~ col ~ "), '[^A-Z]', '', 'g')" -%}
    {%- set coded = "translate(" ~ cleaned ~ ", 'AEIOUHWYBFPVCGJKQSXZDTLMNR', '00000000111122222222334556')" -%}
    {%- set ns = namespace(expr=coded) -%}
    {%- for pass_num in range(3) -%}
      {%- for digit in ['0', '1', '2', '3', '4', '5', '6'] -%}
        {%- set ns.expr = "replace(" ~ ns.expr ~ ", '" ~ digit ~ digit ~ "', '" ~ digit ~ "')" -%}
      {%- endfor -%}
    {%- endfor -%}
    {%- set digits_only = "replace(substr(" ~ ns.expr ~ ", 2), '0', '')" -%}
    CASE
      WHEN {{ cleaned }} IS NULL OR {{ cleaned }} = '' THEN NULL
      ELSE substr({{ cleaned }}, 1, 1) || rpad(left({{ digits_only }}, 3), 3, '0')
    END
  {%- endif -%}
{% endmacro %}
