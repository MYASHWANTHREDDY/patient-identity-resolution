{{ config(materialized='table') }}

{#
  Asymmetric join, not a self-join like candidate_pairs.sql: every match-path record against
  every core record sharing a (blocking_pass, block_key), restricted to non-oversized core
  blocks (matchpath_block_stats.sql). record_key_a/record_key_b (not
  matchpath_record_key/core_record_key) so spark_jobs/score_pairs.py -- which only knows
  about "record_key_a, record_key_b, patient_normalized-shaped table" -- reads this table
  completely unmodified, joining record_key_a and record_key_b against
  conformance.patient_normalized_with_matchpath (the union that has both populations).
#}

with eligible_blocks as (
    select blocking_pass, block_key
    from {{ ref('matchpath_block_stats') }}
    where not is_oversized
)

select distinct
    m.record_key as record_key_a,
    c.record_key as record_key_b,
    m.blocking_pass,
    m.block_key
from {{ ref('matchpath_block_keys') }} m
join {{ ref('matchpath_block_keys') }} c
    on m.blocking_pass = c.blocking_pass
    and m.block_key = c.block_key
    and c.side = 'core'
join eligible_blocks e
    on e.blocking_pass = m.blocking_pass
    and e.block_key = m.block_key
where m.side = 'matchpath'
