import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mdm.backends.local import load_tier_to_duckdb
from mdm.evaluate import load_pair_noise_type, run_blocking_evaluation

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
def test_blocking_metrics_end_to_end(tmp_path):
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
            # serving/* sources are tables scripts/run_matching.py writes -- not present
            # yet on a fresh database. See docs/design-decisions.md, two-phase dbt flow.
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

    true_pairs = set(load_pair_noise_type(db_path))
    blocking_result = run_blocking_evaluation("ci", db_path, true_pairs)

    unioned = blocking_result["by_pass"]["unioned"]
    assert unioned["reduction_ratio"] > 0.99
    assert 0.0 <= unioned["pair_completeness"] <= 1.0

    # unioning passes can only help or match the best single pass, never hurt
    per_pass_pc = [
        stats["pair_completeness"]
        for name, stats in blocking_result["by_pass"].items()
        if name != "unioned"
    ]
    assert unioned["pair_completeness"] >= max(per_pass_pc)

    # uncapped candidate set can only be a superset of the capped one
    uncapped_pc = blocking_result["uncapped_pair_completeness"]
    capped_pc = blocking_result["capped_pair_completeness"]
    assert uncapped_pc >= capped_pc
    assert blocking_result["pair_completeness_cost_of_cap"] >= 0.0
