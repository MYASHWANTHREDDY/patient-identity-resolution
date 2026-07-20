{{ config(materialized='table') }}

{#
  Layer 2, fact grain (Phase 19). One row per fill, never deduplicated. pbm_member_id stays
  unresolved here -- resolving it to patient_global_id via vendor_id_map is Path B linking
  logic (docs/domain-linking-strategy.md), which belongs in the serving-layer fct model
  (fct_pharmacy_claims.sql), the same way patient_normalized never touches the crosswalk
  either. rx_key compounds {vendor}:{source_rx_id} for the same reason claim_key does in
  medical_claims_normalized.sql.
#}

with unioned as (
    select 'VENDOR_A' as source_vendor, *
    from {{ source('raw_standard', 'vendor_a_pharmacy_claims') }}
    union all
    select 'VENDOR_C' as source_vendor, *
    from {{ source('raw_standard', 'vendor_c_pharmacy_claims') }}
)

select
    source_vendor,
    source_vendor || ':' || source_rx_id as rx_key,
    source_rx_id,
    pbm_member_id,
    cast(fill_date as date) as fill_date,
    ndc_code,
    days_supply,
    quantity
from unioned
