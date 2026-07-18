{#
  REVISED in Phase 11: this used to dispatch to BigQuery's native SOUNDEX() vs a hand-rolled
  DuckDB equivalent. Phase 11's real parity check (docs/design-decisions.md) found they
  disagree on real data -- e.g. SOUNDEX('SANCHEZ') is 'S520' on BigQuery but the textbook
  Soundex algorithm (and DuckDB's hand-rolled version) gives 'S522'. BigQuery's native
  SOUNDEX doesn't document its exact algorithm, so matching it precisely isn't practical --
  instead, the SAME hand-rolled algorithm now runs on both targets, verified identical via
  `TRANSLATE`/`SUBSTR`/`RPAD`/`REPLACE`, which behave identically on DuckDB and BigQuery.
  P8: "divergence between tiers is a defect" -- cross-backend determinism wins over using
  whatever a given engine ships natively.

  The only remaining dispatch is `regexp_replace`'s flag argument: DuckDB requires an
  explicit 'g' flag for a global replace (without it, only the first match is replaced);
  BigQuery's REGEXP_REPLACE has no flag argument and is already global by default (passing
  a 4th argument there is a signature error, not a no-op -- see strip_non_digits.sql).

  Collapse-adjacent-duplicates deliberately avoids regex backreferences (`(.)\1+`) -- both
  DuckDB (reproduced independently of dbt) and BigQuery's RE2 engine (which doesn't support
  backreferences in patterns at all, only in replacements) reject or mishandle that pattern.
  Plain literal `replace()` calls, one per digit class, three passes deep (collapses runs up
  to length 8 -- names never produce runs anywhere near that long), sidestep it entirely.
#}
{% macro phonetic_key(col) %}
  {%- if target.type == 'bigquery' -%}
    {%- set cleaned = "REGEXP_REPLACE(UPPER(" ~ col ~ "), '[^A-Z]', '')" -%}
  {%- else -%}
    {%- set cleaned = "regexp_replace(upper(" ~ col ~ "), '[^A-Z]', '', 'g')" -%}
  {%- endif -%}
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
    ELSE substr({{ cleaned }}, 1, 1) || rpad(substr({{ digits_only }}, 1, 3), 3, '0')
  END
{% endmacro %}
