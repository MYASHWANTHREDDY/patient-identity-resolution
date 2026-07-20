{{ config(materialized='view') }}

{#
  The golden record plus everything that backs it: every source system's identifier for
  this person, a field_lineage row count, and (Phase 21) a cross-domain summary over Phase
  19's join-path fact tables (medical_history, medical_claims, pharmacy_claims) and Phase
  20's match-path fact tables (pharmacy_info, lab_results). "The golden record is the
  headline; the crosswalk is the product" (PROJECT_CONSTITUTION.md #9) -- this view is the
  crosswalk made queryable in one row per person, now with a per-domain "how much do we
  know about this person, and how fresh is it" summary alongside it.

  Stays a live view, recomputed per query -- the decision made at Phase 13 and confirmed at
  Phase 17 -- so active_prescription_count (fill_date + days_supply >= current_date) is
  deliberately not point-in-time frozen the way a snapshot would be: it reflects "as of
  right now", same as everything else in this view.

  No PHI-heavy detail gets flattened in here -- diagnosis codes, drug NDCs, individual lab
  results stay in their fct_* tables, queried directly when a full drill-down is actually
  needed. This view answers "how much, how recent" per domain, not "what exactly."

  Each domain gets its own CTE, pre-aggregated to at most one row per patient_global_id,
  so the final select is a chain of one-to-one left joins -- no fan-out, no GROUP BY needed
  (a deliberate change from the pre-Phase-21 version of this view, which joined
  member_alternate_identifier and field_lineage directly and relied on
  array_agg_distinct/count(DISTINCT ...) plus a GROUP BY to collapse the resulting fan-out;
  same result, less to get wrong once a third and fourth one-to-many join joined the mix).
#}

with alternate_ids as (
    select
        patient_global_id,
        {{ array_agg_distinct("source_vendor || ':' || source_record_id") }} as alternate_identifiers
    from {{ source('serving_written', 'member_alternate_identifier') }}
    group by 1
),

lineage_counts as (
    select
        patient_global_id,
        count(distinct field_name || ':' || record_key) as field_lineage_rows
    from {{ source('serving_written', 'field_lineage') }}
    group by 1
),

medical_history_summary as (
    select
        patient_global_id,
        count(*) as encounter_count,
        max(encounter_date) as most_recent_encounter_date
    from {{ ref('fct_medical_history') }}
    group by 1
),

medical_claims_summary as (
    select
        patient_global_id,
        count(*) as medical_claim_count,
        max(claim_date) as most_recent_claim_date
    from {{ ref('fct_medical_claims') }}
    group by 1
),

pharmacy_claims_summary as (
    select
        patient_global_id,
        count(*) as pharmacy_claim_count,
        max(fill_date) as most_recent_fill_date,
        count(case when fill_date + cast(days_supply as integer) >= current_date then 1 end)
            as active_prescription_count
    from {{ ref('fct_pharmacy_claims') }}
    group by 1
),

pharmacy_info_summary as (
    -- Match-path (Phase 20): NOT guaranteed one row per patient_global_id, even though
    -- each source pharmacy_info record maps to at most one identity at generation time --
    -- two *different* pharmacy_info records can still both auto-match to the same
    -- patient_global_id (a real precision limit of probabilistic matching, not a bug: two
    -- similar-enough people, or a core cluster that split into more than one PGID and
    -- each half separately attracted a match). Found at dev tier (17 collisions) when
    -- this CTE assumed 1:1 and unique_member_360_patient_global_id failed -- grouped
    -- explicitly now, same as every fact-grain domain below, rather than trusted
    -- implicitly. pharmacy_info_match_count surfaces the collision instead of hiding it.
    select
        patient_global_id,
        max(plan_tier) as pharmacy_plan_tier,
        count(*) as pharmacy_info_match_count
    from {{ ref('fct_pharmacy_info') }}
    group by 1
),

lab_results_summary as (
    select
        patient_global_id,
        count(*) as lab_result_count,
        max(test_date) as most_recent_lab_date,
        count(case when abnormal_flag != 'normal' then 1 end) as abnormal_lab_count
    from {{ ref('fct_lab_results') }}
    group by 1
)

select
    d.patient_global_id,
    d.first_name,
    d.last_name,
    d.dob,
    d.gender,
    d.ssn_last4,
    m.source_record_count,
    ai.alternate_identifiers,
    coalesce(lc.field_lineage_rows, 0) as field_lineage_rows,
    coalesce(mh.encounter_count, 0) as encounter_count,
    mh.most_recent_encounter_date,
    coalesce(mc.medical_claim_count, 0) as medical_claim_count,
    mc.most_recent_claim_date,
    coalesce(pc.pharmacy_claim_count, 0) as pharmacy_claim_count,
    pc.most_recent_fill_date,
    coalesce(pc.active_prescription_count, 0) as active_prescription_count,
    pi.pharmacy_plan_tier,
    coalesce(pi.pharmacy_info_match_count, 0) as pharmacy_info_match_count,
    coalesce(lr.lab_result_count, 0) as lab_result_count,
    lr.most_recent_lab_date,
    coalesce(lr.abnormal_lab_count, 0) as abnormal_lab_count
from {{ source('serving_written', 'member_demographics') }} d
join {{ source('serving_written', 'membership') }} m
    on m.patient_global_id = d.patient_global_id
left join alternate_ids ai on ai.patient_global_id = d.patient_global_id
left join lineage_counts lc on lc.patient_global_id = d.patient_global_id
left join medical_history_summary mh on mh.patient_global_id = d.patient_global_id
left join medical_claims_summary mc on mc.patient_global_id = d.patient_global_id
left join pharmacy_claims_summary pc on pc.patient_global_id = d.patient_global_id
left join pharmacy_info_summary pi on pi.patient_global_id = d.patient_global_id
left join lab_results_summary lr on lr.patient_global_id = d.patient_global_id
