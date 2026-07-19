"""BigQuery backend: the read/write boundary for scripts/run_matching_bigquery.py, the
Dataproc/BigQuery-backed sibling of mdm.pipeline.run_matching (PROJECT_CONSTITUTION.md #8).
By the time this module's functions run, spark_jobs/score_pairs.py and
spark_jobs/cluster_identities.py have already populated matching.pair_scores and
matching.clusters at scale -- everything here operates on record-count-sized data (patient
records, clusters, crosswalk entries), never pair-count-sized data, so plain BigQuery
client calls are enough; nothing here needs Spark.
"""

from __future__ import annotations

import pandas as pd

from mdm.crosswalk import CrosswalkEntry
from mdm.survivorship import FIELDS as SURVIVORSHIP_FIELDS

PATIENT_NORMALIZED_COLUMNS = (
    "record_key",
    "source_vendor",
    "source_record_id",
    "first_name",
    "last_name",
    "dob",
    "gender",
    "ssn",
    "normalized_at",
)


def read_patient_normalized(client, project: str) -> pd.DataFrame:
    query = (
        f"SELECT {', '.join(PATIENT_NORMALIZED_COLUMNS)} "
        f"FROM `{project}.conformance.patient_normalized`"
    )
    return client.query(query).to_dataframe()


def read_clusters(client, project: str) -> pd.DataFrame:
    """One row per connected component from spark_jobs/cluster_identities.py's output --
    already carries the same flagged/confidence/size fields as mdm.clustering.Cluster."""
    query = (
        "SELECT component, members, size, scored_pairs, possible_pairs, confidence, flagged "
        f"FROM `{project}.matching.clusters`"
    )
    return client.query(query).to_dataframe()


def read_pair_scores(client, project: str, *, lower: float, upper: float) -> pd.DataFrame:
    """Only pairs in the review band [lower, upper) -- the sole use of pair_scores outside
    Dataproc scoring/clustering is routing borderline pairs to review_queue, and unlike
    patient_normalized/clusters, pair_scores is pair-count-scaled (hundreds of millions of
    rows at the scale tier, not record-count-scaled) -- downloading the whole table to
    filter client-side doesn't fit in memory once real data volume is involved (see
    docs/design-decisions.md, Phase 14). lower/upper match triage.decide()'s REVIEW
    condition exactly, so every row this query returns already belongs in review_pairs."""
    query = (
        f"SELECT record_key_a, record_key_b, score FROM `{project}.matching.pair_scores` "
        f"WHERE score >= {lower} AND score < {upper}"
    )
    return client.query(query).to_dataframe()


def read_existing_crosswalk(client, project: str) -> dict[str, CrosswalkEntry]:
    """Empty dict on a fresh project -- same "table doesn't exist yet" handling as
    mdm.pipeline's DuckDB-specific _load_existing_crosswalk, via INFORMATION_SCHEMA instead
    of duckdb's information_schema.tables (BigQuery's dataset-scoped equivalent)."""
    exists = client.query(
        f"SELECT 1 FROM `{project}.serving.INFORMATION_SCHEMA.TABLES` "
        "WHERE table_name = 'crosswalk'"
    ).to_dataframe()
    if exists.empty:
        return {}

    df = client.query(
        "SELECT record_key, patient_global_id, first_seen_run, last_seen_run "
        f"FROM `{project}.serving.crosswalk`"
    ).to_dataframe()
    return {
        row.record_key: CrosswalkEntry(
            row.record_key, row.patient_global_id, row.first_seen_run, row.last_seen_run
        )
        for row in df.itertuples()
    }


def _load_dataframe(
    client, project: str, dataset_table: str, df: pd.DataFrame, *, disposition: str
) -> None:
    from google.cloud import bigquery

    job_config = bigquery.LoadJobConfig(write_disposition=disposition)
    job = client.load_table_from_dataframe(df, f"{project}.{dataset_table}", job_config=job_config)
    job.result()


def write_crosswalk_and_events(
    client,
    project: str,
    new_crosswalk: dict[str, CrosswalkEntry],
    identity_events: list,
    review_pairs: list[tuple[str, str, float]],
    run_id: str,
) -> None:
    """The three serving tables that don't scale with golden-record count the way
    member_demographics/field_lineage/etc. do (see write_serving_batch): crosswalk is one
    row per record_key (record-count-scaled, not cluster-count-scaled, and needed whole for
    resolve_crosswalk's cross-cluster split/merge state -- see docs/design-decisions.md,
    Phase 14), identity_events only has rows for actual create/merge/split events, and
    review_pairs is already filtered to a small band server-side (read_pair_scores). None of
    these needed batching to fit in memory; only the golden-record construction below did.
    Same replace-vs-append semantics as mdm.pipeline._write_tables (see the comment there on
    why identity_events appends while crosswalk/etc. replace wholesale each run)."""
    crosswalk_df = pd.DataFrame(
        [
            {
                "record_key": e.record_key,
                "patient_global_id": e.patient_global_id,
                "first_seen_run": e.first_seen_run,
                "last_seen_run": e.last_seen_run,
            }
            for e in new_crosswalk.values()
        ]
    )
    _load_dataframe(
        client, project, "serving.crosswalk", crosswalk_df, disposition="WRITE_TRUNCATE"
    )

    event_columns = ["event_type", "surviving_id", "retired_id", "run_id", "reason"]
    if identity_events:
        events_df = pd.DataFrame(
            [
                {
                    "event_type": e.event_type,
                    "surviving_id": e.surviving_id,
                    "retired_id": e.retired_id,
                    "run_id": e.run_id,
                    "reason": e.reason,
                }
                for e in identity_events
            ]
        )
    else:
        events_df = pd.DataFrame(columns=event_columns)
    _load_dataframe(
        client, project, "serving.identity_events", events_df, disposition="WRITE_APPEND"
    )

    review_columns = ["record_key_a", "record_key_b", "score", "status", "run_id"]
    if review_pairs:
        review_df = pd.DataFrame(
            [
                {
                    "record_key_a": a,
                    "record_key_b": b,
                    "score": score,
                    "status": "pending",
                    "run_id": run_id,
                }
                for a, b, score in review_pairs
            ]
        )
    else:
        review_df = pd.DataFrame(columns=review_columns)
    _load_dataframe(
        client, project, "serving.review_queue", review_df, disposition="WRITE_TRUNCATE"
    )


def write_serving_batch(
    client,
    project: str,
    golden_records: list[dict],
    field_lineage_rows: list,
    alternate_ids: list[dict],
    membership_rows: list[dict],
    *,
    is_first_batch: bool,
) -> None:
    """member_demographics/field_lineage/member_alternate_identifier/membership, one call per
    golden-record batch (see scripts/run_matching_bigquery.py's pgid batching). Building all
    of a scale-tier run's golden records/lineage in one pass -- and building records_by_key
    for all 5M source records to do it -- grew past 11GB resident and was still climbing
    before it could write anything (Phase 14, see docs/design-decisions.md). WRITE_TRUNCATE
    on the first batch (clearing whatever the previous run left, same as the old single
    unbatched write), WRITE_APPEND on every batch after -- together reproducing the same
    'replace wholesale each run' semantics without ever holding all golden records in memory
    at once. Called at least once even for an empty run, so a run that legitimately produces
    zero golden records still truncates stale data from a previous run."""
    disposition = "WRITE_TRUNCATE" if is_first_batch else "WRITE_APPEND"

    demographics_columns = [
        "patient_global_id",
        *[f for f in SURVIVORSHIP_FIELDS if f != "ssn"],
        "ssn_last4",
    ]
    if golden_records:
        demographics_df = pd.DataFrame(golden_records)[demographics_columns]
    else:
        demographics_df = pd.DataFrame(columns=demographics_columns)
    _load_dataframe(
        client, project, "serving.member_demographics", demographics_df, disposition=disposition
    )

    if field_lineage_rows:
        lineage_df = pd.DataFrame(
            [
                {
                    "patient_global_id": row.patient_global_id,
                    "field_name": row.field_name,
                    "winning_value": str(row.winning_value)
                    if row.winning_value is not None
                    else None,
                    "source_vendor": row.source_vendor,
                    "source_record_id": row.source_record_id,
                    "record_key": row.record_key,
                    "rule_applied": row.rule_applied,
                }
                for row in field_lineage_rows
            ]
        )
    else:
        lineage_df = pd.DataFrame(
            columns=[
                "patient_global_id",
                "field_name",
                "winning_value",
                "source_vendor",
                "source_record_id",
                "record_key",
                "rule_applied",
            ]
        )
    _load_dataframe(client, project, "serving.field_lineage", lineage_df, disposition=disposition)

    alt_ids_df = pd.DataFrame(
        alternate_ids, columns=["patient_global_id", "source_vendor", "source_record_id"]
    )
    _load_dataframe(
        client, project, "serving.member_alternate_identifier", alt_ids_df, disposition=disposition
    )

    membership_df = pd.DataFrame(
        membership_rows, columns=["patient_global_id", "source_record_count"]
    )
    _load_dataframe(client, project, "serving.membership", membership_df, disposition=disposition)
