{{
  config(
    materialized='table',
    cluster_by=['last_name_phonetic', 'dob'] if target.type == 'bigquery' else none
  )
}}

{#
  Clustering on the blocking key isn't cosmetic -- it's what makes the Phase 5 blocking
  self-join cheap on BigQuery, since matching rows co-locate on disk (PROJECT_CONSTITUTION.md
  #9). DuckDB has no equivalent physical clustering config, so this is a no-op there.
#}

with unioned as (
    select * from {{ ref('stg_vendor_a') }}
    union all
    select * from {{ ref('stg_vendor_b') }}
    union all
    select * from {{ ref('stg_vendor_c') }}
)

select
    source_vendor,
    source_record_id,
    record_key,
    first_name,
    last_name,
    dob,
    gender,
    ssn,
    {{ phonetic_key('first_name') }} as first_name_phonetic,
    {{ phonetic_key('last_name') }} as last_name_phonetic,
    extract(year from dob) as dob_year,
    cast(floor(extract(year from dob) / 10) * 10 as integer) as dob_decade,
    current_timestamp as normalized_at
from unioned
