-- Singular test: every conformance-layer pharmacy_claims record must resolve to a
-- patient_global_id, surviving *both* Path B joins (pbm_member_id -> vendor_id_map_normalized
-- -> the crosswalk) -- the two-hop path is exactly where a silent drop is most likely if
-- either join's key doesn't line up (PROJECT_CONSTITUTION.md #14).
with counts as (
    select
        (select count(*) from {{ ref('pharmacy_claims_normalized') }}) as source_count,
        (select count(*) from {{ ref('fct_pharmacy_claims') }}) as fact_count
)
select *
from counts
where source_count != fact_count
