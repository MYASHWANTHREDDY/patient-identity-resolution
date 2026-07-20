-- Singular test: every conformance-layer medical_claims record must resolve to a
-- patient_global_id (PROJECT_CONSTITUTION.md #14).
with counts as (
    select
        (select count(*) from {{ ref('medical_claims_normalized') }}) as source_count,
        (select count(*) from {{ ref('fct_medical_claims') }}) as fact_count
)
select *
from counts
where source_count != fact_count
