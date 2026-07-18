#!/usr/bin/env python
"""BigQuery-backed sibling of scripts/run_quality_checks.py -- Tier 2 statistical quality
checks (PROJECT_CONSTITUTION.md #14), run after scripts/run_matching_bigquery.py has
populated serving.*. Reuses mdm.quality.run_all_checks unchanged (PROJECT_CONSTITUTION.md
#8); only the read/write boundary differs. Never halts the pipeline -- Tier 1 structural
checks are dbt tests and already halted the DAG upstream if something was structurally wrong.

    python scripts/run_quality_checks_bigquery.py --project patient-dedup-mdm
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import pandas as pd

from mdm.quality import run_all_checks


def run_quality_checks_bigquery(project: str, *, run_id: str | None = None) -> pd.DataFrame:
    from google.cloud import bigquery

    run_id = run_id or datetime.now(UTC).isoformat()
    client = bigquery.Client(project=project)

    total_source_records = next(
        client.query(
            f"SELECT count(*) AS n FROM `{project}.conformance.patient_normalized`"
        ).result()
    ).n
    total_golden_records = next(
        client.query(f"SELECT count(*) AS n FROM `{project}.serving.member_demographics`").result()
    ).n
    block_stats = client.query(
        f"SELECT blocking_pass, record_count FROM `{project}.matching.block_stats`"
    ).to_dataframe()
    patient_normalized = client.query(
        f"SELECT dob_year FROM `{project}.conformance.patient_normalized`"
    ).to_dataframe()
    membership = client.query(
        f"SELECT source_record_count FROM `{project}.serving.membership`"
    ).to_dataframe()
    review_count = next(
        client.query(f"SELECT count(*) AS n FROM `{project}.serving.review_queue`").result()
    ).n
    total_scored_pairs = next(
        client.query(
            "SELECT count(DISTINCT record_key_a || ':' || record_key_b) AS n "
            f"FROM `{project}.matching.candidate_pairs`"
        ).result()
    ).n

    results = run_all_checks(
        total_source_records=total_source_records,
        total_golden_records=total_golden_records,
        review_count=review_count,
        total_scored_pairs=total_scored_pairs,
        block_stats=block_stats,
        patient_normalized=patient_normalized,
        membership=membership,
    )

    results_df = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "check_name": r.check_name,
                "status": r.status,
                "value": r.value,
                "message": r.message,
                "checked_at": run_id,
            }
            for r in results
        ]
    )

    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    job = client.load_table_from_dataframe(
        results_df, f"{project}.quality.validation_runs", job_config=job_config
    )
    job.result()

    return results_df


def main(argv: list[str] | None = None) -> pd.DataFrame:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args(argv)

    results_df = run_quality_checks_bigquery(args.project, run_id=args.run_id)

    warn_count = (results_df["status"] == "warn").sum()
    print(f"project={args.project} checks={len(results_df)} warnings={warn_count}")
    for _, row in results_df.iterrows():
        print(f"  [{row['status'].upper():4s}] {row['check_name']}: {row['message']}")

    return results_df


if __name__ == "__main__":
    main()
