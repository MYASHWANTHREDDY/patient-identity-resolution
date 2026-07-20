-- Singular test: same guard as assert_no_candidate_pairs_from_oversized_blocks.sql, for the
-- asymmetric match-path join -- no candidate pair should come from a core-side block that
-- exceeds max_block_size.
select cp.record_key_a, cp.record_key_b, cp.blocking_pass, cp.block_key
from {{ ref('matchpath_candidate_pairs') }} cp
join {{ ref('matchpath_block_stats') }} bs
    on bs.blocking_pass = cp.blocking_pass
    and bs.block_key = cp.block_key
where bs.is_oversized
