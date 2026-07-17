#!/usr/bin/env python
"""Run the matching pipeline (scoring -> triage -> clustering -> crosswalk ->
survivorship) against a tier's DuckDB, writing serving.* tables.

Requires `dbt build` to have already populated conformance.patient_normalized and
matching.candidate_pairs, and config/fs_params.yml to exist (scripts/estimate_fs_params.py).

    python scripts/run_matching.py --tier dev
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mdm.config import REPO_ROOT, VALID_TIERS
from mdm.pipeline import run_matching


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    args = parser.parse_args(argv)

    db_path = args.db_path or (REPO_ROOT / "data" / args.tier / "mdm.duckdb")
    summary = run_matching(str(db_path), run_id=args.run_id)

    print(
        f"tier={args.tier} run_id={summary['run_id']} records={summary['num_records']} "
        f"auto_match_edges={summary['num_auto_match_edges']} clusters={summary['num_clusters']} "
        f"flagged={summary['num_flagged_clusters']} golden_records={summary['num_golden_records']} "
        f"identity_events={summary['num_identity_events']}"
    )
    return summary


if __name__ == "__main__":
    main()
