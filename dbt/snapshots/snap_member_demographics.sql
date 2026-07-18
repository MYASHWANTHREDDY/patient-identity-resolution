{#
  SCD2 history of the golden record (PROJECT_CONSTITUTION.md #10). A survivorship rule can
  flip a surviving value between runs, or a merge/split event can rewrite which records back
  a patient_global_id -- "what did this patient's golden record look like in March?" is a
  real MDM question, and `check` strategy over the demographic columns is what answers it
  without needing a reliable updated_at timestamp on the source (which member_demographics,
  written by a single-batch Python pipeline, doesn't have).
#}
{% snapshot snap_member_demographics %}

{{
    config(
      target_schema='snapshots',
      unique_key='patient_global_id',
      strategy='check',
      check_cols=['first_name', 'last_name', 'dob', 'gender', 'ssn_last4'],
    )
}}

select * from {{ source('serving_written', 'member_demographics') }}

{% endsnapshot %}
