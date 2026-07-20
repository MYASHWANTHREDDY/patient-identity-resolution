{{ config(materialized='table') }}

{#
  Layer 2 pass-through for Path B's per-vendor id map (docs/domain-linking-strategy.md):
  pbm_member_id -> that same vendor's own enrollment record_id. Only VENDOR_A and VENDOR_C
  appear here (both Path B for pharmacy_claims); a vendor never maps to itself twice, so no
  union-time key-collision handling is needed the way the fact tables above need it.
#}

select
    source_vendor,
    pbm_member_id,
    enrollment_member_id
from {{ source('raw_standard', 'vendor_id_map') }}
