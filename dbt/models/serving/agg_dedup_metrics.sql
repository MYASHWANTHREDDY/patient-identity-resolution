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
    select
        count(*) filter (where event_type = 'create') as create_events,
        count(*) filter (where event_type = 'merge') as merge_events,
        count(*) filter (where event_type = 'split') as split_events
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
    1.0 - (golden_counts.total_golden_records::double
        / nullif(source_counts.total_source_records, 0)) as dedup_rate,
    event_counts.create_events,
    event_counts.merge_events,
    event_counts.split_events,
    cluster_sizes.max_cluster_size,
    cluster_sizes.avg_cluster_size,
    current_timestamp as computed_at
from source_counts, golden_counts, event_counts, cluster_sizes
