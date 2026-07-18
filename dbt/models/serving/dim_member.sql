{{ config(materialized='view') }}

select
    d.patient_global_id,
    d.first_name,
    d.last_name,
    d.dob,
    d.gender,
    d.ssn_last4,
    m.source_record_count
from {{ source('serving_written', 'member_demographics') }} d
join {{ source('serving_written', 'membership') }} m
    on m.patient_global_id = d.patient_global_id
