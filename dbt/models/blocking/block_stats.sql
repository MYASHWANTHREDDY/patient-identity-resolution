{{ config(materialized='table') }}

{#
  Block size distribution per pass -- the skew analysis (PROJECT_CONSTITUTION.md #12).
  A block over max_block_size is excluded from candidate_pairs entirely, not capped/sampled:
  the multi-pass union is what's expected to cover the loss, and Phase 14's scale run
  measures whether that expectation actually holds at 5M.
#}

select
    blocking_pass,
    block_key,
    count(*) as record_count,
    count(*) > {{ var('max_block_size') }} as is_oversized
from {{ ref('block_keys') }}
group by 1, 2
