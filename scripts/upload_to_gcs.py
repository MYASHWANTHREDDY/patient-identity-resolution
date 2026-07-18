#!/usr/bin/env python
"""Upload a tier's generated Parquet to GCS -- the cloud-tier equivalent of
scripts/load_local.py's read_parquet() straight into DuckDB (PROJECT_CONSTITUTION.md #9).
Requires the gcloud CLI authenticated with write access to the bucket
(`gcloud auth login`); the Terraform-provisioned bucket name is `<project_id>-mdm-raw`.

    python scripts/upload_to_gcs.py --tier dev --bucket patient-dedup-mdm-mdm-raw
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from mdm.config import REPO_ROOT, VALID_TIERS


def upload_tier_to_gcs(tier_dir: Path, bucket: str, tier: str) -> str:
    if not tier_dir.exists():
        raise FileNotFoundError(
            f"No generated data at {tier_dir} -- run scripts/generate.py first"
        )

    # On Windows, `gcloud` resolves to gcloud.cmd -- subprocess.run(..., shell=False)
    # can't launch a .cmd wrapper by bare name via CreateProcess, so resolve the actual
    # executable path via shutil.which (finds gcloud.cmd on Windows, gcloud on POSIX).
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        raise FileNotFoundError("gcloud CLI not found on PATH")

    destination = f"gs://{bucket}/{tier}"
    # rsync (not cp) so the *contents* of tier_dir (raw/, ground_truth/) land directly
    # under the tier prefix, matching #9's layout exactly -- cp would nest an extra
    # directory level.
    subprocess.run(
        [gcloud, "storage", "rsync", "--recursive", str(tier_dir), destination],
        check=True,
    )
    return destination


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, required=True)
    parser.add_argument(
        "--bucket", required=True, help="GCS bucket name, e.g. <project_id>-mdm-raw"
    )
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    args = parser.parse_args(argv)

    tier_dir = args.data_dir / args.tier
    destination = upload_tier_to_gcs(tier_dir, args.bucket, args.tier)
    print(f"tier={args.tier} uploaded to {destination}")
    return destination


if __name__ == "__main__":
    main()
