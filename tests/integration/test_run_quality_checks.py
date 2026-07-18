import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from mdm.backends.local import load_tier_to_duckdb
from mdm.comparators import build_nickname_index
from mdm.evaluate import load_pair_noise_type, load_records_by_key
from mdm.fs_estimation import estimate_fs_params
from mdm.pipeline import run_matching
from run_quality_checks import run_quality_checks  # noqa: E402

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


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_quality_checks_persist_to_validation_runs(tmp_path):
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

    full_result = _dbt_build(profiles_dir, db_path)
    assert full_result.returncode == 0, full_result.stdout + full_result.stderr

    results_df = run_quality_checks(str(db_path), run_id="qc-run1")
    assert len(results_df) == 5
    assert set(results_df["status"]) <= {"pass", "warn"}

    con = duckdb.connect(str(db_path), read_only=True)
    persisted_count = con.execute(
        "SELECT count(*) FROM quality.validation_runs WHERE run_id = 'qc-run1'"
    ).fetchone()[0]
    con.close()
    assert persisted_count == 5

    # a second run appends rather than overwriting -- validation history accumulates
    run_quality_checks(str(db_path), run_id="qc-run2")
    con = duckdb.connect(str(db_path), read_only=True)
    total_count = con.execute("SELECT count(*) FROM quality.validation_runs").fetchone()[0]
    con.close()
    assert total_count == 10
