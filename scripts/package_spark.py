#!/usr/bin/env python
"""Package everything a Dataproc Serverless batch needs beyond the base runtime image:
dist/mdm.zip (this project's own code) and dist/rapidfuzz.zip (the one third-party
dependency mdm.comparators needs -- see docs/design-decisions.md, Phase 12, for why this is
a downloaded Linux wheel rather than a gcloud pip-install property: no such property was
found to actually work for Dataproc Serverless batches, only for persistent clusters).

    python scripts/package_spark.py
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_MDM = REPO_ROOT / "src" / "mdm"
DIST_DIR = REPO_ROOT / "dist"
MDM_ZIP = DIST_DIR / "mdm.zip"
RAPIDFUZZ_ZIP = DIST_DIR / "rapidfuzz.zip"

# Must match the Dataproc Serverless runtime's Python version (2.2.x runtimes use 3.12) and
# rapidfuzz's exact pin in requirements.txt -- rapidfuzz 3.14.5 only ships a
# manylinux_2_27/2_28 wheel for cp312, not the older manylinux2014 (glibc 2.17) baseline.
DATAPROC_PYTHON_VERSION = "312"
DATAPROC_PLATFORM = "manylinux_2_28_x86_64"
RAPIDFUZZ_VERSION = "3.14.5"


def package_mdm() -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(MDM_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in SRC_MDM.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            zf.write(path, arcname=path.relative_to(REPO_ROOT / "src"))
    return MDM_ZIP


def package_rapidfuzz() -> Path:
    """Downloads the prebuilt Linux wheel (no local compilation, works from any dev OS) and
    ships it as-is -- a wheel's internal layout (package importable from its zip root) is
    already exactly what Spark's --py-files expects from a .zip."""
    DIST_DIR.mkdir(exist_ok=True)
    download_dir = DIST_DIR / "_rapidfuzz_wheel"
    download_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            f"rapidfuzz=={RAPIDFUZZ_VERSION}",
            "--no-deps",
            "--only-binary=:all:",
            f"--platform={DATAPROC_PLATFORM}",
            f"--python-version={DATAPROC_PYTHON_VERSION}",
            "--implementation=cp",
            f"--abi=cp{DATAPROC_PYTHON_VERSION}",
            "-d",
            str(download_dir),
        ],
        check=True,
    )
    wheels = list(download_dir.glob("rapidfuzz-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one rapidfuzz wheel, found {wheels}")
    wheels[0].replace(RAPIDFUZZ_ZIP)
    return RAPIDFUZZ_ZIP


def main() -> None:
    print(f"wrote {package_mdm()}")
    print(f"wrote {package_rapidfuzz()}")


if __name__ == "__main__":
    main()
