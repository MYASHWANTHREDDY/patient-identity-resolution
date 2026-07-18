#!/usr/bin/env python
"""Load a tier's GCS Parquet into BigQuery `raw_standard` -- the cloud-tier equivalent of
scripts/load_local.py's read_parquet() straight into DuckDB. BigQuery load jobs are free
(PROJECT_CONSTITUTION.md #7); run scripts/upload_to_gcs.py first so the source Parquet
exists in the bucket.

    python scripts/load_bigquery.py --tier dev --project patient-dedup-mdm \
        --bucket patient-dedup-mdm-mdm-raw
"""

from __future__ import annotations

import argparse
import shutil
import subprocess

from mdm.config import VALID_TIERS

VENDOR_TABLES = ("vendor_a", "vendor_b", "vendor_c")


def load_tier_to_bigquery(project: str, bucket: str, tier: str) -> dict[str, str]:
    # Same shutil.which() resolution as upload_to_gcs.py -- bq is also a .cmd wrapper on
    # Windows and subprocess.run(shell=False) can't launch it by bare name there.
    bq = shutil.which("bq")
    if bq is None:
        raise FileNotFoundError("bq CLI not found on PATH")

    destinations: dict[str, str] = {}
    for vendor_table in VENDOR_TABLES:
        source = f"gs://{bucket}/{tier}/raw/{vendor_table}/part-*.parquet"
        destination = f"{project}:raw_standard.{vendor_table}"
        subprocess.run(
            [
                bq,
                "load",
                "--source_format=PARQUET",
                "--replace",  # idempotent: truncate + reload, matching load_local.py's
                # CREATE OR REPLACE TABLE (P7 -- safe to re-run)
                destination,
                source,
            ],
            check=True,
        )
        destinations[vendor_table] = destination

    return destinations


def main(argv: list[str] | None = None) -> dict[str, str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args(argv)

    destinations = load_tier_to_bigquery(args.project, args.bucket, args.tier)
    for vendor_table, destination in destinations.items():
        print(f"tier={args.tier} loaded {vendor_table} -> {destination}")
    return destinations


if __name__ == "__main__":
    main()
