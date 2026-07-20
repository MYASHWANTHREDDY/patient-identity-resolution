#!/usr/bin/env python
"""BigQuery-backed sibling of scripts/run_matchpath_matching.py (mdm.pipeline.
run_matchpath_matching) -- resolves pharmacy_info/lab_identity (Phase 20, no shared ID with
anything) to a patient_global_id at scale, after dbt has built
matching.matchpath_candidate_pairs and spark_jobs/score_pairs.py has scored them into
matching.matchpath_pair_scores. Reuses the exact same triage decision (mdm.triage.decide)
as every other matching path in this project -- only the read/write boundary differs.

Requires scripts/run_matching_bigquery.py to have already run: this resolves match-path
records *against* the core population's crosswalk, so running it first is meaningless (same
precondition mdm.pipeline.run_matchpath_matching enforces locally).

    python scripts/run_matchpath_matching_bigquery.py --project patient-dedup-mdm
"""

from __future__ import annotations

import argparse

from mdm.backends.bigquery import (
    read_best_matchpath_auto_matches,
    read_best_matchpath_review_candidates,
    read_existing_crosswalk,
    write_matchpath_tables,
)
from mdm.pipeline import load_fs_params, load_nickname_index, load_thresholds

# record_key prefix -> domain name, the BigQuery-script equivalent of the domain tracking
# mdm.pipeline.run_matchpath_matching gets for free from which conformance table a row came
# from (it queries pharmacy_info_normalized/lab_identity_normalized separately). Here, a
# scored pair only carries record_key_a as a plain string, so the domain has to be recovered
# from its prefix -- the same convention src/mdm/generator/matchpath.py mints record_keys
# with ("VENDOR_B_PHARMACY:{source_record_id}", "VENDOR_D:{source_record_id}").
_DOMAIN_BY_PREFIX = {
    "VENDOR_B_PHARMACY:": "pharmacy_info",
    "VENDOR_D:": "lab_identity",
}


def domain_for_record_key(record_key: str) -> str:
    for prefix, domain in _DOMAIN_BY_PREFIX.items():
        if record_key.startswith(prefix):
            return domain
    raise ValueError(
        f"record_key {record_key!r} doesn't start with a known match-path domain prefix "
        f"({sorted(_DOMAIN_BY_PREFIX)})"
    )


def run_matchpath_matching_bigquery(
    project: str,
    *,
    fs_params: dict | None = None,
    nickname_index: dict[str, str] | None = None,
) -> dict:
    from google.cloud import bigquery

    fs_params = fs_params if fs_params is not None else load_fs_params()
    nickname_index = nickname_index if nickname_index is not None else load_nickname_index()
    thresholds = load_thresholds()
    client = bigquery.Client(project=project)

    existing_crosswalk = read_existing_crosswalk(client, project)
    if not existing_crosswalk:
        raise RuntimeError(
            "serving.crosswalk is empty or missing -- run scripts/run_matching_bigquery.py "
            "before resolving match-path records."
        )
    record_to_pgid = {rk: entry.patient_global_id for rk, entry in existing_crosswalk.items()}

    # Best candidate per match-path record_key, computed server-side (see
    # mdm.backends.bigquery._read_best_matchpath_candidate) -- never a client-side
    # groupby/max over the full pair-count-scaled scores table.
    auto_match_df = read_best_matchpath_auto_matches(client, project, upper=thresholds["upper"])
    review_df = read_best_matchpath_review_candidates(
        client, project, lower=thresholds["lower"], upper=thresholds["upper"]
    )

    resolution_rows = []
    for row in auto_match_df.itertuples(index=False):
        pgid = record_to_pgid.get(row.record_key_b)
        if pgid is None:
            continue  # defensive: every core record should have a crosswalk entry
        resolution_rows.append(
            {
                "domain": domain_for_record_key(row.record_key_a),
                "record_key": row.record_key_a,
                "source_record_id": row.record_key_a.split(":", 1)[1],
                "patient_global_id": pgid,
                "matched_core_record_key": row.record_key_b,
                "match_score": float(row.score),
            }
        )

    auto_matched_keys = {row["record_key"] for row in resolution_rows}
    review_rows = [
        {
            "domain": domain_for_record_key(row.record_key_a),
            "record_key": row.record_key_a,
            "candidate_core_record_key": row.record_key_b,
            "score": float(row.score),
            "status": "pending",
        }
        for row in review_df.itertuples(index=False)
        if row.record_key_a not in auto_matched_keys
    ]

    write_matchpath_tables(client, project, resolution_rows, review_rows)

    return {
        "num_auto_matched": len(resolution_rows),
        "num_review": len(review_rows),
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    args = parser.parse_args(argv)

    summary = run_matchpath_matching_bigquery(args.project)

    print(
        f"project={args.project} auto_matched={summary['num_auto_matched']} "
        f"review={summary['num_review']}"
    )
    return summary


if __name__ == "__main__":
    main()
