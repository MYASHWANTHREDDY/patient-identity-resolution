import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mdm.backends.local import load_tier_to_duckdb
from mdm.comparators import build_nickname_index
from mdm.evaluate import load_pair_noise_type, load_records_by_key, run_scoring_evaluation
from mdm.fs_estimation import estimate_fs_params

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
def test_scoring_evaluation_end_to_end(tmp_path):
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

    records_by_key = load_records_by_key(db_path)
    true_pairs = set(load_pair_noise_type(db_path))

    with (REPO_ROOT / "config" / "nicknames.yml").open("r", encoding="utf-8") as f:
        nickname_table = yaml.safe_load(f) or {}
    nickname_index = build_nickname_index(nickname_table)

    fs_params = estimate_fs_params(
        records_by_key, true_pairs, sample_size=2000, seed=42, nickname_index=nickname_index
    )

    plot_path = tmp_path / "pr_curve.png"
    scoring_result = run_scoring_evaluation(
        "ci", db_path, true_pairs, fs_params, nickname_index, plot_path=plot_path
    )

    assert 0.0 <= scoring_result["fs_best_f1"] <= 1.0
    assert 0.0 <= scoring_result["naive_best_f1"] <= 1.0
    assert scoring_result["lower_threshold"] <= scoring_result["upper_threshold"]
    assert plot_path.exists()

    total = sum(scoring_result["decision_counts"].values())
    assert total == scoring_result["num_candidate_pairs"]
