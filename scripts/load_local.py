#!/usr/bin/env python
"""Load a tier's generated Parquet into local DuckDB `raw_standard` -- the local-backend
equivalent of a BigQuery load job.

    python scripts/load_local.py --tier dev
"""

from __future__ import annotations

import argparse
from pathlib import Path

from mdm.backends.local import load_tier_to_duckdb
from mdm.config import REPO_ROOT, VALID_TIERS


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, required=True)
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--db-path", type=Path, default=None)
    args = parser.parse_args(argv)

    tier_dir = args.data_dir / args.tier
    db_path = args.db_path or (tier_dir / "mdm.duckdb")

    counts = load_tier_to_duckdb(tier_dir, db_path)
    print(f"tier={args.tier} db={db_path} counts={counts}")
    return counts


if __name__ == "__main__":
    main()
