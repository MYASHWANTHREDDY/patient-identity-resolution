{{ config(materialized='table') }}

{#
  Layer 2, fact grain (Phase 19). One row per claim, never deduplicated. claim_key
  compounds {vendor}:{source_claim_id} for the same reason encounter_key does in
  medical_history_normalized.sql -- source_claim_id alone isn't globally unique across
  vendors.
#}

with unioned as (
    select 'VENDOR_A' as source_vendor, *
    from {{ source('raw_standard', 'vendor_a_medical_claims') }}
    union all
    select 'VENDOR_B' as source_vendor, *
    from {{ source('raw_standard', 'vendor_b_medical_claims') }}
)

select
    source_vendor,
    source_vendor || ':' || source_claim_id as claim_key,
    source_claim_id,
    member_id,
    cast(claim_date as date) as claim_date,
    diagnosis_code,
    procedure_code,
    billed_amount,
    paid_amount,
    claim_status
from unioned
