#!/usr/bin/env python
"""Run Tier 2 statistical quality checks and persist results to quality.validation_runs
(PROJECT_CONSTITUTION.md #14). Tier 1 structural checks are dbt tests (`dbt build`) and
halt the pipeline on failure; this script never halts -- it warns and records.

Requires `dbt build` and scripts/run_matching.py to have already populated conformance.*,
matching.block_stats, and serving.* .

    python scripts/run_quality_checks.py --tier dev
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from mdm.config import REPO_ROOT, VALID_TIERS
from mdm.quality import run_all_checks


def run_quality_checks(db_path: str, *, run_id: str | None = None) -> pd.DataFrame:
    run_id = run_id or datetime.now(UTC).isoformat()
    con = duckdb.connect(db_path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS quality")

        total_source_records = con.execute(
            "SELECT count(*) FROM conformance.patient_normalized"
        ).fetchone()[0]
        total_golden_records = con.execute(
            "SELECT count(*) FROM serving.member_demographics"
        ).fetchone()[0]
        block_stats = con.execute(
            "SELECT blocking_pass, record_count FROM matching.block_stats"
        ).df()
        patient_normalized = con.execute(
            "SELECT dob_year FROM conformance.patient_normalized"
        ).df()
        membership = con.execute("SELECT source_record_count FROM serving.membership").df()

        review_count = con.execute("SELECT count(*) FROM serving.review_queue").fetchone()[0]
        total_scored_pairs = con.execute(
            "SELECT count(DISTINCT record_key_a || ':' || record_key_b) "
            "FROM matching.candidate_pairs"
        ).fetchone()[0]

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

        con.execute(
            "CREATE TABLE IF NOT EXISTS quality.validation_runs ("
            "run_id VARCHAR, check_name VARCHAR, status VARCHAR, "
            "value DOUBLE, message VARCHAR, checked_at VARCHAR)"
        )
        con.register("results_df", results_df)
        con.execute("INSERT INTO quality.validation_runs SELECT * FROM results_df")

        return results_df
    finally:
        con.close()


def main(argv: list[str] | None = None) -> pd.DataFrame:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args(argv)

    db_path = args.db_path or (REPO_ROOT / "data" / args.tier / "mdm.duckdb")
    results_df = run_quality_checks(str(db_path), run_id=args.run_id)

    warn_count = (results_df["status"] == "warn").sum()
    print(f"tier={args.tier} checks={len(results_df)} warnings={warn_count}")
    for _, row in results_df.iterrows():
        print(f"  [{row['status'].upper():4s}] {row['check_name']}: {row['message']}")

    return results_df


if __name__ == "__main__":
    main()
