{{ config(materialized='table') }}

{#
  Layer 2, fact grain (Phase 20, Path A once VENDOR_D's lab_identity resolves to a
  patient_global_id -- see fct_lab_results.sql). One row per test result, never
  deduplicated, keyed off lab_identity_normalized.source_record_id the same way Phase 19's
  pharmacy_claims_normalized keys off a pbm_member_id: a fact that only becomes joinable
  after its parent identity record is resolved. No natural per-result id in Layer 1
  (src/mdm/generator/matchpath.py never mints one), so lab_result_key is a row_number
  surrogate scoped to source_record_id -- unique here, but not meant to be a stable
  cross-run identifier the way record_key/encounter_key are.
#}

select
    source_record_id || ':' || cast(
        row_number() over (
            partition by source_record_id order by test_date, test_code, result_value
        ) as varchar
    ) as lab_result_key,
    source_record_id,
    cast(test_date as date) as test_date,
    test_code,
    cast(result_value as double) as result_value,
    result_unit,
    abnormal_flag
from {{ source('raw_standard', 'lab_results') }}
