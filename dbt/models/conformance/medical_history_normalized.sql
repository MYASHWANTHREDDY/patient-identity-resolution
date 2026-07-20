{{ config(materialized='table') }}

{#
  Layer 2, fact grain (Phase 19, PROJECT_CONSTITUTION.md): one row per encounter, never
  deduplicated, never survived -- facts aren't golden records (docs/domain-linking-strategy.md).
  source_encounter_id is only unique *within* a vendor (src/mdm/generator/facts.py doesn't
  namespace it by vendor), so encounter_key adds the same {vendor}:{id} compounding
  patient_normalized.record_key already uses, needed here because two vendors' generators
  can independently produce the same source_encounter_id for two different real encounters.
#}

with unioned as (
    select 'VENDOR_A' as source_vendor, *
    from {{ source('raw_standard', 'vendor_a_medical_history') }}
    union all
    select 'VENDOR_C' as source_vendor, *
    from {{ source('raw_standard', 'vendor_c_medical_history') }}
)

select
    source_vendor,
    source_vendor || ':' || source_encounter_id as encounter_key,
    source_encounter_id,
    member_id,
    cast(encounter_date as date) as encounter_date,
    condition_code,
    encounter_type
from unioned
