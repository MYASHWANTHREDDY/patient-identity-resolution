-- Singular test: fct_lab_results must never contain more rows than lab_results_normalized
-- -- same "may shrink, may never grow" conservation bound as
-- assert_conservation_pharmacy_info_no_fanout.sql, applied to the fact-grain domain (many
-- test results per lab_identity) instead of the member-grain one.
with counts as (
    select
        (select count(*) from {{ ref('lab_results_normalized') }}) as source_count,
        (select count(*) from {{ ref('fct_lab_results') }}) as fact_count
)
select *
from counts
where fact_count > source_count
