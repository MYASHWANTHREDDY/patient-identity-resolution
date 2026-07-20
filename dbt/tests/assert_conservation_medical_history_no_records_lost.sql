-- Singular test: every conformance-layer medical_history record must resolve to a
-- patient_global_id -- fct_medical_history.sql's inner join must never silently drop a
-- record because its member_id didn't resolve through the crosswalk (PROJECT_CONSTITUTION.md
-- #14, same conservation principle as
-- assert_conservation_source_records_match_alternate_identifiers.sql).
with counts as (
    select
        (select count(*) from {{ ref('medical_history_normalized') }}) as source_count,
        (select count(*) from {{ ref('fct_medical_history') }}) as fact_count
)
select *
from counts
where source_count != fact_count
