#!/usr/bin/env python
"""Resolve Phase 20's match-path domains (pharmacy_info, lab_identity) to an existing
patient_global_id via real matching against conformance.patient_normalized.

Requires scripts/run_matching.py to have already run (serving.crosswalk must exist) and
`dbt build --exclude path:models/serving` to have populated
conformance.{pharmacy_info,lab_identity}_normalized.

    python scripts/run_matchpath_matching.py --tier dev
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mdm.config import REPO_ROOT, VALID_TIERS
from mdm.pipeline import run_matchpath_matching


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--db-path", type=Path, default=None)
    args = parser.parse_args(argv)

    db_path = args.db_path or (REPO_ROOT / "data" / args.tier / "mdm.duckdb")
    summary = run_matchpath_matching(str(db_path))

    print(
        f"tier={args.tier} matchpath_records={summary['num_matchpath_records']} "
        f"candidate_pairs={summary['num_candidate_pairs']} "
        f"auto_matched={summary['num_auto_matched']} review={summary['num_review']} "
        f"unmatched={summary['num_unmatched']}"
    )
    return summary


if __name__ == "__main__":
    main()
