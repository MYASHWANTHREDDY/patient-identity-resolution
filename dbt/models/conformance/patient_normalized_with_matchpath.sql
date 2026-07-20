{{ config(materialized='table') }}

{#
  Scale-tier match-path scoring (Phase 20, extended to BigQuery): spark_jobs/score_pairs.py
  joins candidate_pairs' record_key_a/record_key_b against *one* patient_normalized-shaped
  table (mdm.backends.spark.join_pairs_with_records broadcasts it twice, once per side) --
  reused completely unmodified for match-path by pointing it at this table instead, which is
  patient_normalized itself plus the two match-path domains, minimal columns only (record_key
  + the five comparator fields; see mdm.scoring.FIELDS). matchpath_candidate_pairs.sql's
  record_key_a is always a match-path key and record_key_b is always a core key, so every
  pair finds both sides here regardless of which population a given key belongs to -- the
  same reason pharmacy_info_normalized/lab_identity_normalized were shaped identically to
  patient_normalized in the first place (docs/domain-linking-strategy.md).
#}

select record_key, first_name, last_name, dob, ssn, gender
from {{ ref('patient_normalized') }}

union all

select record_key, first_name, last_name, dob, ssn, gender
from {{ ref('pharmacy_info_normalized') }}

union all

select record_key, first_name, last_name, dob, ssn, gender
from {{ ref('lab_identity_normalized') }}
