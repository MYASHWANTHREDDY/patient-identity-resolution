import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DBT_PROJECT_DIR = REPO_ROOT / "dbt"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


# Console scripts land in .venv/Scripts/*.exe on Windows but .venv/bin/* on POSIX. Checking
# only the Windows layout skipped every dbt-backed test in this file on Linux whenever the
# venv wasn't already on PATH -- a green run that had actually executed nothing.
_VENV_DBT = REPO_ROOT / ".venv" / ("Scripts/dbt.exe" if os.name == "nt" else "bin/dbt")


def _dbt_available() -> bool:
    return shutil.which("dbt") is not None or _VENV_DBT.exists()


def _dbt_executable() -> str:
    return str(_VENV_DBT) if _VENV_DBT.exists() else "dbt"


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_dbt_build_ci_tier_conformance(tmp_path):
    data_dir = tmp_path / "data"
    subprocess.run(
        [
            sys.executable,
            str(GENERATE_SCRIPT),
            "--tier",
            "ci",
            "--seed",
            "42",
            "--workers",
            "1",
            "--shard-size",
            "400",
            "--out-dir",
            str(data_dir),
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    from mdm.backends.local import load_tier_to_duckdb

    db_path = tmp_path / "mdm.duckdb"
    load_tier_to_duckdb(data_dir / "ci", db_path)

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    shutil.copy(DBT_PROJECT_DIR / "profiles.yml.example", profiles_dir / "profiles.yml")

    env = {**os.environ, "DUCKDB_PATH": str(db_path)}
    result = subprocess.run(
        [
            _dbt_executable(),
            "build",
            "--project-dir",
            str(DBT_PROJECT_DIR),
            "--profiles-dir",
            str(profiles_dir),
            "--target",
            "dev",
            # serving/* sources are tables scripts/run_matching.py writes -- they don't
            # exist yet on a fresh database, so this build (conformance + blocking only)
            # must exclude them. See docs/design-decisions.md on the two-phase dbt flow.
            "--exclude",
            "path:models/serving",
            "snap_member_demographics",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row_count = con.execute("SELECT count(*) FROM conformance.patient_normalized").fetchone()[0]
        gt_count = con.execute("SELECT count(*) FROM ground_truth.ground_truth").fetchone()[0]
        assert row_count == gt_count
        assert row_count > 0
    finally:
        con.close()
