{{ config(materialized='table') }}

{#
  Self-join per pass, restricted to non-oversized blocks. record_key_a < record_key_b keeps
  each unordered pair to a single row. A pair can appear more than once here (once per pass
  that found it) -- Phase 6 scoring dedupes across passes; this table is blocking's raw
  output, kept pass-attributed for the RR/PC metrics below.

  Always a full table, not `incremental`, even on the target where the constitution lists
  candidate_pairs as incremental (BigQuery, to avoid recomputing a 250M-row self-join every
  run at 5M). A true incremental strategy is scale-run infrastructure -- premature before
  Phase 14 actually makes its cost concrete and measurable (see docs/design-decisions.md).
#}

with eligible_blocks as (
    select blocking_pass, block_key
    from {{ ref('block_stats') }}
    where not is_oversized
)

select
    k1.record_key as record_key_a,
    k2.record_key as record_key_b,
    k1.blocking_pass,
    k1.block_key
from {{ ref('block_keys') }} k1
join {{ ref('block_keys') }} k2
    on k1.blocking_pass = k2.blocking_pass
    and k1.block_key = k2.block_key
    and k1.record_key < k2.record_key
join eligible_blocks e
    on e.blocking_pass = k1.blocking_pass
    and e.block_key = k1.block_key
