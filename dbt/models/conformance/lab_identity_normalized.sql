{{ config(materialized='table') }}

{#
  Layer 2, match-path (Phase 20, PROJECT_CONSTITUTION.md): VENDOR_D, a lab with no
  eligibility relationship to any member at all -- the one domain in the vendor matrix
  with no ID to lean on and no other domain from the same vendor to join through
  (docs/domain-linking-strategy.md). Shaped identically to pharmacy_info_normalized /
  patient_normalized for the same reason: real matching against patient_normalized, not a
  join.
#}

select
    'VENDOR_D' as source_vendor,
    source_record_id,
    'VENDOR_D:' || source_record_id as record_key,
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
    current_timestamp as normalized_at
from {{ source('raw_standard', 'lab_identity') }}
