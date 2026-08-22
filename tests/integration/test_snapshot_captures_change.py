"""Phase 8 exit criteria: 'Snapshot captures a change.' snap_member_demographics is SCD2
(check strategy) over the golden record -- this proves a changed demographic value actually
produces a second history row, not just that the snapshot runs without error."""

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


# Console scripts land in .venv/Scripts/*.exe on Windows but .venv/bin/* on POSIX. Checking
# only the Windows layout skipped every dbt-backed test in this file on Linux whenever the
# venv wasn't already on PATH -- a green run that had actually executed nothing.
_VENV_DBT = REPO_ROOT / ".venv" / ("Scripts/dbt.exe" if os.name == "nt" else "bin/dbt")


def _dbt_available() -> bool:
    return shutil.which("dbt") is not None or _VENV_DBT.exists()


def _dbt_executable() -> str:
    return str(_VENV_DBT) if _VENV_DBT.exists() else "dbt"


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
def test_snapshot_captures_a_changed_golden_record(tmp_path):
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
    # run_matchpath_matching, a separate step from core run_matching
    # (docs/domain-linking-strategy.md).
    run_matchpath_matching(str(db_path), fs_params=fs_params, nickname_index=nickname_index)

    first_build = _dbt_build(profiles_dir, db_path)
    assert first_build.returncode == 0, first_build.stdout + first_build.stderr

    con = duckdb.connect(str(db_path))
    pgid = con.execute(
        "SELECT patient_global_id FROM serving.member_demographics LIMIT 1"
    ).fetchone()[0]
    rows_before = con.execute(
        "SELECT count(*) FROM snapshots.snap_member_demographics WHERE patient_global_id = ?",
        [pgid],
    ).fetchone()[0]
    assert rows_before == 1

    # simulate a survivorship rule flipping a surviving value on a later run
    con.execute(
        "UPDATE serving.member_demographics SET first_name = 'CHANGED_BY_TEST' "
        "WHERE patient_global_id = ?",
        [pgid],
    )
    con.close()

    second_build = _dbt_build(profiles_dir, db_path)
    assert second_build.returncode == 0, second_build.stdout + second_build.stderr

    con = duckdb.connect(str(db_path), read_only=True)
    history = con.execute(
        "SELECT first_name, dbt_valid_to FROM snapshots.snap_member_demographics "
        "WHERE patient_global_id = ? ORDER BY dbt_valid_from",
        [pgid],
    ).fetchall()
    con.close()

    assert len(history) == 2  # the change produced a second SCD2 row
    assert history[0][1] is not None  # first version now has a closed valid_to
    assert history[1][0] == "CHANGED_BY_TEST"  # newest version reflects the change
    assert history[1][1] is None  # newest version is still open
