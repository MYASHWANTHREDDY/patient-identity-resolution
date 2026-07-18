select
    'VENDOR_A' as source_vendor,
    record_id as source_record_id,
    'VENDOR_A:' || record_id as record_key,
    upper(trim(first_name)) as first_name,
    upper(trim(last_name)) as last_name,
    {{ parse_vendor_date('dob', '%Y-%m-%d') }} as dob,
    case upper(trim(gender)) when 'M' then 'M' when 'F' then 'F' else 'U' end as gender,
    nullif({{ strip_non_digits('ssn') }}, '') as ssn
from {{ source('raw_standard', 'vendor_a') }}
