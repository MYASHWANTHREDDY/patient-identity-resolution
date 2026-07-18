{{ config(materialized='table') }}

{#
  Dashboard-facing aggregates (Phase 9's Overview tab). One row per build, so
  quality.validation_runs / the dashboard can show these alongside review-queue and
  blocking stats without recomputing raw counts from serving.* every page load.
#}

with source_counts as (
    select count(*) as total_source_records
    from {{ ref('patient_normalized') }}
),

golden_counts as (
    select count(*) as total_golden_records
    from {{ source('serving_written', 'member_demographics') }}
),

event_counts as (
    -- sum(case when ...) over filter(where ...): the latter is DuckDB/Postgres syntax,
    -- not valid on BigQuery -- this model is only ever built against BigQuery from
    -- dedup_dag's dbt_run_serving task (Phase 11's parity check deliberately excludes
    -- models/serving, so this dialect gap went undetected until Phase 13's first real
    -- run -- see docs/design-decisions.md).
    -- coalesce(sum(...), 0): sum() of zero rows is NULL, not 0, unlike count(*) filter
    -- (where ...) which returns 0 for an empty table -- without this, a fresh table with
    -- no identity_events yet turns every column here NULL and crashes the dashboard's
    -- int(metrics["create_events"]) cast on a NaN.
    select
        coalesce(sum(case when event_type = 'create' then 1 else 0 end), 0) as create_events,
        coalesce(sum(case when event_type = 'merge' then 1 else 0 end), 0) as merge_events,
        coalesce(sum(case when event_type = 'split' then 1 else 0 end), 0) as split_events
    from {{ source('serving_written', 'identity_events') }}
),

cluster_sizes as (
    select
        max(source_record_count) as max_cluster_size,
        avg(source_record_count) as avg_cluster_size
    from {{ source('serving_written', 'membership') }}
)

select
    source_counts.total_source_records,
    golden_counts.total_golden_records,
    -- no explicit cast needed: `/` is true division (never integer floor division) in
    -- both DuckDB and BigQuery, and the `1.0 -` already forces float arithmetic even so.
    1.0 - (golden_counts.total_golden_records
        / nullif(source_counts.total_source_records, 0)) as dedup_rate,
    event_counts.create_events,
    event_counts.merge_events,
    event_counts.split_events,
    cluster_sizes.max_cluster_size,
    cluster_sizes.avg_cluster_size,
    current_timestamp as computed_at
from source_counts, golden_counts, event_counts, cluster_sizes
