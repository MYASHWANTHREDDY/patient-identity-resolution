"""Phase 8 exit criteria: 'Injected violation fails.' Structural quality gates are dbt
tests -- this proves they actually catch a corrupted database, not just a clean one."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from mdm.backends.local import load_tier_to_duckdb
from mdm.comparators import build_nickname_index
from mdm.evaluate import load_pair_noise_type, load_records_by_key
from mdm.fs_estimation import estimate_fs_params
from mdm.pipeline import run_matching, run_matchpath_matching

REPO_ROOT = Path(__file__).resolve().parents[2]
DBT_PROJECT_DIR = REPO_ROOT / "dbt"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


def _dbt_available() -> bool:
    return shutil.which("dbt") is not None or (REPO_ROOT / ".venv" / "Scripts" / "dbt.exe").exists()


def _dbt_executable() -> str:
    venv_dbt = REPO_ROOT / ".venv" / "Scripts" / "dbt.exe"
    return str(venv_dbt) if venv_dbt.exists() else "dbt"


def _dbt_build(profiles_dir: Path, db_path: Path, *extra_args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DUCKDB_PATH": str(db_path)}
    return subprocess.run(
        [
            _dbt_executable(),
            "build",
            "--project-dir",
            str(DBT_PROJECT_DIR),
            "--profiles-dir",
            str(profiles_dir),
            "--target",
            "dev",
            *extra_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _build_fully_matched_ci_tier_db(tmp_path: Path) -> Path:
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

    pre_result = _dbt_build(
        profiles_dir, db_path, "--exclude", "path:models/serving", "snap_member_demographics"
    )
    assert pre_result.returncode == 0, pre_result.stdout + pre_result.stderr

    records_by_key = load_records_by_key(db_path)
    true_pairs = set(load_pair_noise_type(db_path))
    with (REPO_ROOT / "config" / "nicknames.yml").open("r", encoding="utf-8") as f:
        nickname_table = yaml.safe_load(f) or {}
    nickname_index = build_nickname_index(nickname_table)
    fs_params = estimate_fs_params(
        records_by_key, true_pairs, sample_size=2000, seed=42, nickname_index=nickname_index
    )
    run_matching(str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index)
    # Phase 20: serving.fct_pharmacy_info/fct_lab_results (and their source tests) need
    # serving.matchpath_resolution to exist before the full dbt build below -- written by
    # run_matchpath_matching, a separate step from core run_matching (docs/domain-linking-strategy.md).
    run_matchpath_matching(str(db_path), fs_params=fs_params, nickname_index=nickname_index)

    full_result = _dbt_build(profiles_dir, db_path)
    assert full_result.returncode == 0, full_result.stdout + full_result.stderr

    return db_path, profiles_dir


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_conservation_gate_catches_a_deleted_alternate_identifier(tmp_path):
    db_path, profiles_dir = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path))
    con.execute(
        "DELETE FROM serving.member_alternate_identifier WHERE rowid = "
        "(SELECT min(rowid) FROM serving.member_alternate_identifier)"
    )
    con.close()

    result = _dbt_build(profiles_dir, db_path)
    assert result.returncode != 0
    assert "assert_conservation_source_records_match_alternate_identifiers" in result.stdout


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_uniqueness_gate_catches_a_duplicated_patient_global_id(tmp_path):
    db_path, profiles_dir = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path))
    row = con.execute("SELECT * FROM serving.member_demographics LIMIT 1").fetchone()
    columns = [d[0] for d in con.description]
    other_pgid = con.execute(
        "SELECT patient_global_id FROM serving.member_demographics "
        f"WHERE patient_global_id != '{row[0]}' LIMIT 1"
    ).fetchone()[0]
    duplicate_row = (other_pgid, *row[1:])
    placeholders = ", ".join("?" for _ in columns)
    con.execute(
        f"INSERT INTO serving.member_demographics ({', '.join(columns)}) VALUES ({placeholders})",
        duplicate_row,
    )
    con.close()

    result = _dbt_build(profiles_dir, db_path)
    assert result.returncode != 0
    assert "unique" in result.stdout.lower()


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_crosswalk_completeness_gate_catches_a_missing_record(tmp_path):
    db_path, profiles_dir = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path))
    con.execute(
        "DELETE FROM serving.crosswalk WHERE record_key = "
        "(SELECT record_key FROM serving.crosswalk LIMIT 1)"
    )
    con.close()

    result = _dbt_build(profiles_dir, db_path)
    assert result.returncode != 0
    assert "assert_crosswalk_completeness" in result.stdout
