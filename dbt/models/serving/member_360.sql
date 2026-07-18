{{ config(materialized='view') }}

{#
  The golden record plus everything that backs it: every source system's identifier for
  this person, and a count of how many field_lineage rows contributed. "The golden record
  is the headline; the crosswalk is the product" (PROJECT_CONSTITUTION.md #9) -- this view
  is the crosswalk made queryable in one row per person.
#}

select
    d.patient_global_id,
    d.first_name,
    d.last_name,
    d.dob,
    d.gender,
    d.ssn_last4,
    m.source_record_count,
    {{ array_agg_distinct("a.source_vendor || ':' || a.source_record_id") }} as alternate_identifiers,
    count(distinct l.field_name || ':' || l.record_key) as field_lineage_rows
from {{ source('serving_written', 'member_demographics') }} d
join {{ source('serving_written', 'membership') }} m
    on m.patient_global_id = d.patient_global_id
left join {{ source('serving_written', 'member_alternate_identifier') }} a
    on a.patient_global_id = d.patient_global_id
left join {{ source('serving_written', 'field_lineage') }} l
    on l.patient_global_id = d.patient_global_id
group by 1, 2, 3, 4, 5, 6, 7
