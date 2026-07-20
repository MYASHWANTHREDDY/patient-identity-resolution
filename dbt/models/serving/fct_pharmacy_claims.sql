{{ config(materialized='table') }}

{#
  Path B (docs/domain-linking-strategy.md): pharmacy_claims carries a *different* ID than
  the vendor's own eligibility record (a separate PBM relationship) -- two joins, not one:
  pbm_member_id -> vendor_id_map_normalized -> that vendor's own enrollment_member_id, then
  the same crosswalk join fct_medical_claims/fct_medical_history use directly.
#}

with resolved_to_enrollment_id as (
    select
        p.source_vendor,
        p.rx_key,
        p.fill_date,
        p.ndc_code,
        p.days_supply,
        p.quantity,
        v.enrollment_member_id
    from {{ ref('pharmacy_claims_normalized') }} p
    join {{ ref('vendor_id_map_normalized') }} v
        on v.source_vendor = p.source_vendor
        and v.pbm_member_id = p.pbm_member_id
)

select
    m.patient_global_id,
    r.source_vendor,
    r.rx_key,
    r.fill_date,
    r.ndc_code,
    r.days_supply,
    r.quantity
from resolved_to_enrollment_id r
join {{ source('serving_written', 'member_alternate_identifier') }} m
    on m.source_vendor = r.source_vendor
    and m.source_record_id = r.enrollment_member_id
