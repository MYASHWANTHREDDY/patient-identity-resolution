-- Singular test: record_key_a must always be a match-path record (VENDOR_B_PHARMACY: or
-- VENDOR_D:) and record_key_b must always be a core record (VENDOR_A:/VENDOR_B:/VENDOR_C:)
-- -- matchpath_block_keys.sql's join direction (`m.side = 'matchpath'`, `c.side = 'core'`)
-- is the only thing enforcing this; a self-pair or a reversed pair would silently corrupt
-- the resolve step downstream (a matched core_record_key that's actually a match-path key
-- has no crosswalk entry -- see scripts/run_matchpath_matching_bigquery.py).
select record_key_a, record_key_b, blocking_pass
from {{ ref('matchpath_candidate_pairs') }}
where not (
    (starts_with(record_key_a, 'VENDOR_B_PHARMACY:') or starts_with(record_key_a, 'VENDOR_D:'))
    and (
        starts_with(record_key_b, 'VENDOR_A:')
        or starts_with(record_key_b, 'VENDOR_B:')
        or starts_with(record_key_b, 'VENDOR_C:')
    )
)
