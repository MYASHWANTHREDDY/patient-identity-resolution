-- Singular test: no candidate pair should have been generated from a block that exceeds
-- max_block_size -- oversized blocks are excluded entirely, not capped (PROJECT_CONSTITUTION.md #12).
select cp.record_key_a, cp.record_key_b, cp.blocking_pass, cp.block_key
from {{ ref('candidate_pairs') }} cp
join {{ ref('block_stats') }} bs
    on bs.blocking_pass = cp.blocking_pass
    and bs.block_key = cp.block_key
where bs.is_oversized
