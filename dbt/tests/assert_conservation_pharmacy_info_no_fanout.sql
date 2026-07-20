-- Singular test: fct_pharmacy_info must never contain more rows than
-- pharmacy_info_normalized. Unlike Phase 19's join-path fact tables (guaranteed to lose
-- zero records, tested with exact equality in assert_conservation_medical_history_*.sql),
-- match-path resolution is allowed to lose records -- not every pharmacy_info record
-- auto-matches (docs/domain-linking-strategy.md) -- but it must never gain any. A
-- source_record_id resolving to more than one row here would mean the same raw record
-- somehow matched twice, which schema.yml's unique test on fct_pharmacy_info.source_record_id
-- should already prevent; this row-count bound catches the same failure mode structurally
-- (PROJECT_CONSTITUTION.md #14).
with counts as (
    select
        (select count(*) from {{ ref('pharmacy_info_normalized') }}) as source_count,
        (select count(*) from {{ ref('fct_pharmacy_info') }}) as fact_count
)
select *
from counts
where fact_count > source_count
