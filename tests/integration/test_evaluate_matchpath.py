import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from mdm.backends.local import load_tier_to_duckdb
from mdm.comparators import build_nickname_index
from mdm.evaluate import load_pair_noise_type, load_records_by_key, run_matchpath_evaluation
from mdm.fs_estimation import estimate_fs_params
from mdm.pipeline import run_matching, run_matchpath_matching

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


def _dbt_build(db_path: Path, profiles_dir: Path, *, exclude_serving: bool) -> None:
    env = {**os.environ, "DUCKDB_PATH": str(db_path)}
    cmd = [
        _dbt_executable(),
        "build",
        "--project-dir",
        str(DBT_PROJECT_DIR),
        "--profiles-dir",
        str(profiles_dir),
        "--target",
        "dev",
    ]
    if exclude_serving:
        cmd += ["--exclude", "path:models/serving", "snap_member_demographics"]
    result = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_matchpath_evaluation_returns_none_before_matchpath_matching_runs(tmp_path):
    """run_fact_table_evaluation already has this "optional section" contract for Phase 19;
    run_matchpath_evaluation must have the same one for Phase 20 (mdm.evaluate stays usable
    against a database that predates scripts/run_matchpath_matching.py)."""
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
    _dbt_build(db_path, profiles_dir, exclude_serving=True)

    assert run_matchpath_evaluation("ci", db_path) is None


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_matchpath_evaluation_end_to_end(tmp_path):
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
    _dbt_build(db_path, profiles_dir, exclude_serving=True)

    records_by_key = load_records_by_key(db_path)
    true_pairs = set(load_pair_noise_type(db_path))
    with (REPO_ROOT / "config" / "nicknames.yml").open("r", encoding="utf-8") as f:
        nickname_table = yaml.safe_load(f) or {}
    nickname_index = build_nickname_index(nickname_table)
    fs_params = estimate_fs_params(
        records_by_key, true_pairs, sample_size=2000, seed=42, nickname_index=nickname_index
    )

    run_matching(
        str(db_path), tier="ci", run_id="run1", fs_params=fs_params, nickname_index=nickname_index
    )
    run_matchpath_matching(
        str(db_path), tier="ci", fs_params=fs_params, nickname_index=nickname_index
    )
    _dbt_build(db_path, profiles_dir, exclude_serving=False)

    result = run_matchpath_evaluation("ci", db_path)

    assert result is not None
    assert result["total_records"] > 0
    assert (
        result["num_auto_matched"] + result["num_review"] + result["num_unmatched"]
        == (result["total_records"])
    )
    assert 0.0 <= result["precision"] <= 1.0
    assert 0.0 <= result["recall"] <= 1.0
    assert set(result["by_domain"]) == {"pharmacy_info", "lab_identity"}
    for stats in result["by_domain"].values():
        assert 0.0 <= stats["precision"] <= 1.0
        assert 0.0 <= stats["recall"] <= 1.0
