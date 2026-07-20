{{ config(materialized='table') }}

{#
  Path A (docs/domain-linking-strategy.md): medical_history carries the same member ID as
  that vendor's own eligibility record, so this is a straight join through the crosswalk
  already built by matching -- no new linking logic, no matching pass.
#}

select
    m.patient_global_id,
    h.source_vendor,
    h.encounter_key,
    h.encounter_date,
    h.condition_code,
    h.encounter_type
from {{ ref('medical_history_normalized') }} h
join {{ source('serving_written', 'member_alternate_identifier') }} m
    on m.source_vendor = h.source_vendor
    and m.source_record_id = h.member_id
