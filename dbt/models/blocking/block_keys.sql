{{ config(materialized='table') }}

{#
  One row per (record_key, blocking_pass, block_key) -- the unpivoted form. Passes mirror
  config/matching.yml's `blocking.passes` (kept in sync by hand; that file is the
  documentation/Python-side copy, this is dbt's, see docs/design-decisions.md).

  A pass is skipped for a record when any of its key columns is null -- an absent key can't
  usefully co-locate anything, so it isn't a block, just a row with nothing to join on.
#}

with bp_ssn as (
    select record_key, 'bp_ssn' as blocking_pass, ssn as block_key
    from {{ ref('patient_normalized') }}
    where ssn is not null
),

bp_dob_lname as (
    select record_key, 'bp_dob_lname' as blocking_pass,
        {{ as_string('dob') }} || '|' || last_name_phonetic as block_key
    from {{ ref('patient_normalized') }}
    where dob is not null and last_name_phonetic is not null
),

bp_year_names as (
    select record_key, 'bp_year_names' as blocking_pass,
        {{ as_string('dob_year') }} || '|' || first_name_phonetic || '|' || last_name_phonetic as block_key
    from {{ ref('patient_normalized') }}
    where dob_year is not null and first_name_phonetic is not null and last_name_phonetic is not null
),

bp_coarse as (
    -- dob_year, not dob_decade (Phase 14 finding, see docs/design-decisions.md): at 5M
    -- records, a fixed-cardinality decade-wide key produces blocks in the thousands
    -- (last_name_phonetic x gender x ~12 decades has bounded cardinality, so blocks grow
    -- with population) -- 98% of all candidate pairs at that scale came from this one
    -- pass. bp_year_names already proves dob_year keeps blocks small even at 5M (max
    -- observed block size 76); reusing that same granularity here keeps bp_coarse's
    -- catch-all role (still no first-name requirement, still tolerates day/month DOB
    -- noise) while cutting its blocks by roughly 10x.
    select record_key, 'bp_coarse' as blocking_pass,
        last_name_phonetic || '|' || gender || '|' || {{ as_string('dob_year') }} as block_key
    from {{ ref('patient_normalized') }}
    where last_name_phonetic is not null and gender is not null and dob_year is not null
)

select * from bp_ssn
union all
select * from bp_dob_lname
union all
select * from bp_year_names
union all
select * from bp_coarse
