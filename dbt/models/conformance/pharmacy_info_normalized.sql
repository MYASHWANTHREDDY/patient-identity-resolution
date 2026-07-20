{{ config(materialized='table') }}

{#
  Layer 2, match-path (Phase 20, PROJECT_CONSTITUTION.md): VENDOR_B's PBM relationship,
  member-level, no shared ID with VENDOR_B's own eligibility record
  (docs/domain-linking-strategy.md). Shaped exactly like patient_normalized -- same
  phonetic keys, same dob_year -- because scripts/run_matchpath_matching.py blocks and
  scores these rows against patient_normalized with the unchanged comparator/blocking
  pipeline. ssn is a typed NULL, never generated for this domain (has_ssn_field=False in
  src/mdm/generator/matchpath.py), so bp_ssn's blocking pass -- and the ssn comparator --
  both fall through to "missing" here, contributing zero weight rather than a false
  disagreement (PROJECT_CONSTITUTION.md #11.3).
#}

select
    'VENDOR_B_PHARMACY' as source_vendor,
    source_record_id,
    'VENDOR_B_PHARMACY:' || source_record_id as record_key,
    upper(trim(first_name)) as first_name,
    upper(trim(last_name)) as last_name,
    {{ parse_vendor_date('dob', '%Y-%m-%d') }} as dob,
    case upper(trim(gender)) when 'M' then 'M' when 'F' then 'F' else 'U' end as gender,
    cast(null as varchar) as ssn,
    {{ phonetic_key('first_name') }} as first_name_phonetic,
    {{ phonetic_key('last_name') }} as last_name_phonetic,
    extract(year from {{ parse_vendor_date('dob', '%Y-%m-%d') }}) as dob_year,
    address,
    phone,
    plan_tier,
    current_timestamp as normalized_at
from {{ source('raw_standard', 'pharmacy_info') }}
