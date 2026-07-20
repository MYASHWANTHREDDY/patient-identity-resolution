{{ config(materialized='table') }}

{#
  Path A (docs/domain-linking-strategy.md): medical_claims carries the same member ID as
  that vendor's own eligibility record -- a straight join through the crosswalk, same as
  fct_medical_history.sql.
#}

select
    m.patient_global_id,
    c.source_vendor,
    c.claim_key,
    c.claim_date,
    c.diagnosis_code,
    c.procedure_code,
    c.billed_amount,
    c.paid_amount,
    c.claim_status
from {{ ref('medical_claims_normalized') }} c
join {{ source('serving_written', 'member_alternate_identifier') }} m
    on m.source_vendor = c.source_vendor
    and m.source_record_id = c.member_id
