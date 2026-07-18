#!/usr/bin/env python
"""BigQuery-backed sibling of scripts/run_matching.py (mdm.pipeline.run_matching) --
crosswalk resolution and golden-record survivorship for the cloud pipeline, run *after*
Dataproc has already scored (spark_jobs/score_pairs.py) and clustered
(spark_jobs/cluster_identities.py) at scale. Reuses the exact same pure crosswalk/
survivorship functions as the local DuckDB path (PROJECT_CONSTITUTION.md #8) -- only the
read/write boundary differs, and only record-count-sized data (not pair-count-sized) is
handled here, so plain Python is enough; nothing in this script needs Spark.

    python scripts/run_matching_bigquery.py --project patient-dedup-mdm
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from mdm.backends.bigquery import (
    read_clusters,
    read_existing_crosswalk,
    read_pair_scores,
    read_patient_normalized,
    write_serving_tables,
)
from mdm.clustering import Cluster, finalize_cluster_membership
from mdm.crosswalk import resolve_crosswalk
from mdm.pipeline import (
    build_serving_tables,
    load_thresholds,
    next_crosswalk_sequence,
    sanitize_nan,
)
from mdm.triage import REVIEW, decide


def _clusters_from_dataframe(clusters_df) -> list[Cluster]:
    return [
        Cluster(
            members=tuple(sorted(row.members)),
            scored_pairs=int(row.scored_pairs),
            possible_pairs=int(row.possible_pairs),
            confidence=float(row.confidence),
            flagged=bool(row.flagged),
            flag_reasons=(),
        )
        for row in clusters_df.itertuples()
    ]


def run_matching_bigquery(project: str, *, run_id: str | None = None) -> dict:
    from google.cloud import bigquery

    run_id = run_id or datetime.now(UTC).isoformat()
    thresholds = load_thresholds()
    client = bigquery.Client(project=project)

    patient_normalized_df = read_patient_normalized(client, project)
    clusters_df = read_clusters(client, project)
    pair_scores_df = read_pair_scores(client, project)
    existing_crosswalk = read_existing_crosswalk(client, project)

    records_by_key = patient_normalized_df.set_index("record_key").to_dict(orient="index")
    for key, record in records_by_key.items():
        record["record_key"] = key
    sanitize_nan(records_by_key)

    clusters = _clusters_from_dataframe(clusters_df)
    membership = finalize_cluster_membership(clusters)
    # every record gets a cluster, even ones touched by no auto-match edge at all -- same
    # as mdm.pipeline.run_matching, since connected_components only returns nodes touched
    # by at least one edge (see src/mdm/backends/spark.py).
    for record_key in records_by_key:
        membership.setdefault(record_key, (record_key,))

    review_pairs = [
        (row.record_key_a, row.record_key_b, row.score)
        for row in pair_scores_df.itertuples()
        if decide(row.score, upper=thresholds["upper"], lower=thresholds["lower"]) == REVIEW
    ]

    next_sequence = next_crosswalk_sequence(existing_crosswalk)
    new_crosswalk, identity_events = resolve_crosswalk(
        existing_crosswalk, membership, run_id=run_id, next_sequence=next_sequence
    )

    golden_records, field_lineage_rows, alternate_ids, membership_rows = build_serving_tables(
        new_crosswalk, records_by_key, thresholds
    )

    write_serving_tables(
        client,
        project,
        new_crosswalk,
        identity_events,
        golden_records,
        field_lineage_rows,
        alternate_ids,
        membership_rows,
        review_pairs,
        run_id,
    )

    return {
        "run_id": run_id,
        "num_records": len(records_by_key),
        "num_auto_match_edges": sum(c.scored_pairs for c in clusters),
        "num_review_pairs": len(review_pairs),
        "num_clusters": len(clusters),
        "num_flagged_clusters": sum(1 for c in clusters if c.flagged),
        "num_golden_records": len(golden_records),
        "num_identity_events": len(identity_events),
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args(argv)

    summary = run_matching_bigquery(args.project, run_id=args.run_id)

    print(
        f"project={args.project} run_id={summary['run_id']} records={summary['num_records']} "
        f"auto_match_edges={summary['num_auto_match_edges']} clusters={summary['num_clusters']} "
        f"flagged={summary['num_flagged_clusters']} golden_records={summary['num_golden_records']} "
        f"identity_events={summary['num_identity_events']}"
    )
    return summary


if __name__ == "__main__":
    main()
