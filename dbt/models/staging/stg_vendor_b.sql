select
    'VENDOR_B' as source_vendor,
    record_id as source_record_id,
    'VENDOR_B:' || record_id as record_key,
    upper(trim(fname)) as first_name,
    upper(trim(lname)) as last_name,
    {{ parse_vendor_date('birth_date', '%m/%d/%Y') }} as dob,
    case upper(trim(sex)) when 'M' then 'M' when 'F' then 'F' else 'U' end as gender,
    nullif(regexp_replace(social_security_number, '[^0-9]', '', 'g'), '') as ssn
from {{ source('raw_standard', 'vendor_b') }}
