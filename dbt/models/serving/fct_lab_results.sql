{{ config(materialized='table') }}

{#
  Match-path (Phase 20, docs/domain-linking-strategy.md): a two-step resolution, not a
  join through vendor_id_map_normalized like fct_pharmacy_claims.sql's Path B -- the lab
  identity record itself needs real matching first (serving.matchpath_resolution), then
  each test result attaches to that same identity's source_record_id, mirroring how Phase
  19's pharmacy_claims_normalized attaches to a pbm_member_id. Inner joins throughout: a
  lab identity that never auto-matched has no resolvable patient_global_id, so its test
  results simply don't appear here (same reasoning as fct_pharmacy_info.sql).
#}

with resolved_identity as (
    select
        li.source_record_id,
        r.patient_global_id,
        r.match_score
    from {{ ref('lab_identity_normalized') }} li
    join {{ source('serving_written', 'matchpath_resolution') }} r
        on r.domain = 'lab_identity'
        and r.record_key = li.record_key
)

select
    ri.patient_global_id,
    lr.lab_result_key,
    lr.test_date,
    lr.test_code,
    lr.result_value,
    lr.result_unit,
    lr.abnormal_flag,
    ri.match_score
from {{ ref('lab_results_normalized') }} lr
join resolved_identity ri
    on ri.source_record_id = lr.source_record_id
