#!/usr/bin/env python
"""BigQuery-backed sibling of scripts/run_matching.py (mdm.pipeline.run_matching) --
crosswalk resolution and golden-record survivorship for the cloud pipeline, run *after*
Dataproc has already scored (spark_jobs/score_pairs.py) and clustered
(spark_jobs/cluster_identities.py) at scale. Reuses the exact same pure crosswalk/
survivorship functions as the local DuckDB path (PROJECT_CONSTITUTION.md #8) -- only the
read/write boundary differs.

Golden-record construction/writing is batched by patient_global_id (see _pgid_batches):
at the scale tier, converting all of patient_normalized to a records_by_key dict-of-dicts
in one pass -- the natural, Phase-13-tested way to call build_serving_tables -- grew past
11GB resident and was still climbing before it could write anything (Phase 14, see
docs/design-decisions.md). build_serving_tables itself is unchanged and has no idea it's
being called in batches; only this script's orchestration is new. resolve_crosswalk is NOT
batched -- it needs every cluster's full membership in one pass to correctly detect splits
across the whole run (see mdm.crosswalk), and its input (record_key -> membership tuple) is
far lighter than per-record demographic data, so it was never the memory problem.

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
    write_crosswalk_and_events,
    write_serving_batch,
)
from mdm.clustering import Cluster, finalize_cluster_membership
from mdm.crosswalk import resolve_crosswalk
from mdm.pipeline import (
    build_serving_tables,
    load_thresholds,
    next_crosswalk_sequence,
    sanitize_nan,
)

DEFAULT_BATCH_SIZE = 100_000


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


def _members_by_pgid(new_crosswalk: dict) -> dict[str, list[str]]:
    members_by_pgid: dict[str, list[str]] = {}
    for record_key, entry in new_crosswalk.items():
        members_by_pgid.setdefault(entry.patient_global_id, []).append(record_key)
    return members_by_pgid


def _pgid_batches(members_by_pgid: dict[str, list[str]], batch_size: int):
    """Yields dicts of {pgid: [record_key, ...]}, batch_size pgids at a time -- grouping by
    pgid (not by raw record_key range) so every member of a golden record always lands in
    the same batch. Yields at least one (possibly empty) batch even when members_by_pgid is
    empty, so a run that legitimately produces zero golden records still truncates whatever
    a previous run left in the batched serving tables."""
    items = list(members_by_pgid.items())
    if not items:
        yield {}
        return
    for i in range(0, len(items), batch_size):
        yield dict(items[i : i + batch_size])


def run_matching_bigquery(
    project: str, *, run_id: str | None = None, batch_size: int = DEFAULT_BATCH_SIZE
) -> dict:
    from google.cloud import bigquery

    run_id = run_id or datetime.now(UTC).isoformat()
    thresholds = load_thresholds()
    client = bigquery.Client(project=project)

    # Kept as a DataFrame, never converted to a dict-of-dicts for all rows at once (see
    # module docstring) -- indexed by record_key (drop=False keeps it as a column too, so
    # a batch slice's .to_dict(orient="index") includes it without a manual re-injection
    # loop) so per-batch lookups below are cheap positional slices, not a full rebuild.
    patient_normalized_df = read_patient_normalized(client, project).set_index(
        "record_key", drop=False
    )
    clusters_df = read_clusters(client, project)
    pair_scores_df = read_pair_scores(
        client, project, lower=thresholds["lower"], upper=thresholds["upper"]
    )
    existing_crosswalk = read_existing_crosswalk(client, project)

    clusters = _clusters_from_dataframe(clusters_df)
    membership = finalize_cluster_membership(clusters)
    # every record gets a cluster, even ones touched by no auto-match edge at all -- same
    # as mdm.pipeline.run_matching, since connected_components only returns nodes touched
    # by at least one edge (see src/mdm/backends/spark.py).
    for record_key in patient_normalized_df.index:
        membership.setdefault(record_key, (record_key,))

    # Already filtered to the review band server-side (read_pair_scores' WHERE clause
    # matches triage.decide()'s REVIEW condition exactly) -- every row here belongs.
    review_pairs = [
        (row.record_key_a, row.record_key_b, row.score) for row in pair_scores_df.itertuples()
    ]

    next_sequence = next_crosswalk_sequence(existing_crosswalk)
    new_crosswalk, identity_events = resolve_crosswalk(
        existing_crosswalk, membership, run_id=run_id, next_sequence=next_sequence
    )

    members_by_pgid = _members_by_pgid(new_crosswalk)
    num_golden_records = 0
    for batch_num, pgid_batch in enumerate(_pgid_batches(members_by_pgid, batch_size)):
        batch_record_keys = [rk for record_keys in pgid_batch.values() for rk in record_keys]

        batch_records_by_key = patient_normalized_df.loc[batch_record_keys].to_dict(
            orient="index"
        )
        sanitize_nan(batch_records_by_key)
        batch_new_crosswalk = {rk: new_crosswalk[rk] for rk in batch_record_keys}

        golden_records, field_lineage_rows, alternate_ids, membership_rows = build_serving_tables(
            batch_new_crosswalk, batch_records_by_key, thresholds
        )
        num_golden_records += len(golden_records)

        write_serving_batch(
            client,
            project,
            golden_records,
            field_lineage_rows,
            alternate_ids,
            membership_rows,
            is_first_batch=(batch_num == 0),
        )

    write_crosswalk_and_events(
        client, project, new_crosswalk, identity_events, review_pairs, run_id
    )

    return {
        "run_id": run_id,
        "num_records": len(patient_normalized_df),
        "num_auto_match_edges": sum(c.scored_pairs for c in clusters),
        "num_review_pairs": len(review_pairs),
        "num_clusters": len(clusters),
        "num_flagged_clusters": sum(1 for c in clusters if c.flagged),
        "num_golden_records": num_golden_records,
        "num_identity_events": len(identity_events),
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Patient_global_ids processed per batch. Lower this if memory is still tight; "
        "raise it (or Phase 13's ~50K-record scale needs none of this) for fewer, larger "
        "BigQuery load jobs.",
    )
    args = parser.parse_args(argv)

    summary = run_matching_bigquery(args.project, run_id=args.run_id, batch_size=args.batch_size)

    print(
        f"project={args.project} run_id={summary['run_id']} records={summary['num_records']} "
        f"auto_match_edges={summary['num_auto_match_edges']} clusters={summary['num_clusters']} "
        f"flagged={summary['num_flagged_clusters']} golden_records={summary['num_golden_records']} "
        f"identity_events={summary['num_identity_events']}"
    )
    return summary


if __name__ == "__main__":
    main()
