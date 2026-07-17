select
    'VENDOR_C' as source_vendor,
    record_id as source_record_id,
    'VENDOR_C:' || record_id as record_key,
    upper(trim(given_name)) as first_name,
    upper(trim(surname)) as last_name,
    {{ parse_vendor_date('date_of_birth', '%d-%b-%Y') }} as dob,
    case upper(trim(gender)) when 'M' then 'M' when 'F' then 'F' else 'U' end as gender,
    cast(null as varchar) as ssn  -- Vendor C sends no SSN (PROJECT_CONSTITUTION.md #8)
from {{ source('raw_standard', 'vendor_c') }}
