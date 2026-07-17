-- Singular test: every candidate pair must be a properly ordered, distinct pair
-- (record_key_a < record_key_b) -- guards against self-joins matching a record to itself
-- and against the same unordered pair appearing twice in reversed order.
select record_key_a, record_key_b, blocking_pass
from {{ ref('candidate_pairs') }}
where record_key_a >= record_key_b
