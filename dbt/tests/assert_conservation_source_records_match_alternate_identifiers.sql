-- Singular test: total conformance records must equal total alternate-identifier rows.
-- The single most valuable quality check (PROJECT_CONSTITUTION.md #14) -- it catches
-- records silently lost or duplicated between clustering and survivorship, which would
-- otherwise produce a plausible-looking but wrong answer.
with counts as (
    select
        (select count(*) from {{ ref('patient_normalized') }}) as source_count,
        (select count(*) from {{ source('serving_written', 'member_alternate_identifier') }}) as alt_id_count
)
select *
from counts
where source_count != alt_id_count
