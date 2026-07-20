{{ config(materialized='table') }}

{#
  Scale-tier match-path blocking (Phase 20, extended to BigQuery): the same three non-SSN
  passes as block_keys.sql (match-path records never carry an ssn --
  docs/domain-linking-strategy.md), computed for both the core population
  (`patient_normalized`, `side = 'core'`) and the two match-path domains
  (`pharmacy_info_normalized`/`lab_identity_normalized`, `side = 'matchpath'`), unpivoted
  into one table the same shape as block_keys.sql plus a `side` column --
  matchpath_candidate_pairs.sql joins matchpath-side rows against core-side rows on
  (blocking_pass, block_key), an asymmetric join instead of block_keys.sql's self-join.

  This is a new dbt model (unlike the local/DuckDB tier, where this same logic runs as raw
  SQL directly in Python -- see mdm.pipeline._MATCHPATH_BLOCKING_PASSES and
  docs/design-decisions.md) because at scale the natural shape is already staged --
  BigQuery blocking, Dataproc scoring, a Python resolution step -- the same three stages
  core matching already uses (PROJECT_CONSTITUTION.md #8: the *shape* of orchestration can
  differ by backend; the underlying blocking keys/passes never do).
#}

with core_dob_lname as (
    select record_key, 'core' as side, 'bp_dob_lname' as blocking_pass,
        {{ as_string('dob') }} || '|' || last_name_phonetic as block_key
    from {{ ref('patient_normalized') }}
    where dob is not null and last_name_phonetic is not null
),

core_year_names as (
    select record_key, 'core' as side, 'bp_year_names' as blocking_pass,
        {{ as_string('dob_year') }} || '|' || first_name_phonetic || '|' || last_name_phonetic as block_key
    from {{ ref('patient_normalized') }}
    where dob_year is not null and first_name_phonetic is not null and last_name_phonetic is not null
),

core_coarse as (
    select record_key, 'core' as side, 'bp_coarse' as blocking_pass,
        last_name_phonetic || '|' || gender || '|' || {{ as_string('dob_year') }} as block_key
    from {{ ref('patient_normalized') }}
    where last_name_phonetic is not null and gender is not null and dob_year is not null
),

matchpath_dob_lname as (
    select record_key, 'matchpath' as side, 'bp_dob_lname' as blocking_pass,
        {{ as_string('dob') }} || '|' || last_name_phonetic as block_key
    from {{ ref('pharmacy_info_normalized') }}
    where dob is not null and last_name_phonetic is not null
    union all
    select record_key, 'matchpath' as side, 'bp_dob_lname' as blocking_pass,
        {{ as_string('dob') }} || '|' || last_name_phonetic as block_key
    from {{ ref('lab_identity_normalized') }}
    where dob is not null and last_name_phonetic is not null
),

matchpath_year_names as (
    select record_key, 'matchpath' as side, 'bp_year_names' as blocking_pass,
        {{ as_string('dob_year') }} || '|' || first_name_phonetic || '|' || last_name_phonetic as block_key
    from {{ ref('pharmacy_info_normalized') }}
    where dob_year is not null and first_name_phonetic is not null and last_name_phonetic is not null
    union all
    select record_key, 'matchpath' as side, 'bp_year_names' as blocking_pass,
        {{ as_string('dob_year') }} || '|' || first_name_phonetic || '|' || last_name_phonetic as block_key
    from {{ ref('lab_identity_normalized') }}
    where dob_year is not null and first_name_phonetic is not null and last_name_phonetic is not null
),

matchpath_coarse as (
    select record_key, 'matchpath' as side, 'bp_coarse' as blocking_pass,
        last_name_phonetic || '|' || gender || '|' || {{ as_string('dob_year') }} as block_key
    from {{ ref('pharmacy_info_normalized') }}
    where last_name_phonetic is not null and gender is not null and dob_year is not null
    union all
    select record_key, 'matchpath' as side, 'bp_coarse' as blocking_pass,
        last_name_phonetic || '|' || gender || '|' || {{ as_string('dob_year') }} as block_key
    from {{ ref('lab_identity_normalized') }}
    where last_name_phonetic is not null and gender is not null and dob_year is not null
)

select * from core_dob_lname
union all
select * from core_year_names
union all
select * from core_coarse
union all
select * from matchpath_dob_lname
union all
select * from matchpath_year_names
union all
select * from matchpath_coarse
