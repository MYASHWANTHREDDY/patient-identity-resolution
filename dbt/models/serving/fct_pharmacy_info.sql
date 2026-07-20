{{ config(materialized='table') }}

{#
  Match-path (Phase 20, docs/domain-linking-strategy.md): pharmacy_info has no shared ID
  with anything, so unlike fct_medical_history.sql's Path A join, this joins through
  serving.matchpath_resolution -- real matching output (scripts/run_matchpath_matching.py),
  not a crosswalk lookup. Inner join is deliberate: only auto-matched records appear here,
  the same way Phase 6's review-band pairs never reach a golden record without human
  confirmation (serving.review_queue) -- unmatched/review-pending pharmacy_info records
  simply have no row here, not a null patient_global_id.
#}

select
    r.patient_global_id,
    p.source_record_id,
    p.plan_tier,
    p.address,
    p.phone,
    r.match_score
from {{ ref('pharmacy_info_normalized') }} p
join {{ source('serving_written', 'matchpath_resolution') }} r
    on r.domain = 'pharmacy_info'
    and r.record_key = p.record_key
