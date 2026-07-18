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
from mdm.pipeline import run_matching

REPO_ROOT = Path(__file__).resolve().parents[2]
DBT_PROJECT_DIR = REPO_ROOT / "dbt"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


def _dbt_available() -> bool:
    return shutil.which("dbt") is not None or (REPO_ROOT / ".venv" / "Scripts" / "dbt.exe").exists()


def _dbt_executable() -> str:
    venv_dbt = REPO_ROOT / ".venv" / "Scripts" / "dbt.exe"
    return str(venv_dbt) if venv_dbt.exists() else "dbt"


def _build_ci_tier_db(tmp_path: Path) -> tuple[Path, dict, dict[str, str]]:
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
            # serving/* sources are tables run_matching() is about to write -- not
            # present yet. See docs/design-decisions.md, two-phase dbt flow.
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

    return db_path, fs_params, nickname_index


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matching_is_idempotent(tmp_path):
    db_path, fs_params, nickname_index = _build_ci_tier_db(tmp_path)

    summary_1 = run_matching(
        str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index
    )

    con = duckdb.connect(str(db_path), read_only=True)
    crosswalk_after_run1 = dict(
        con.execute("SELECT record_key, patient_global_id FROM serving.crosswalk").fetchall()
    )
    demographics_after_run1 = con.execute(
        "SELECT * FROM serving.member_demographics ORDER BY patient_global_id"
    ).fetchall()
    con.close()

    summary_2 = run_matching(
        str(db_path), run_id="run2", fs_params=fs_params, nickname_index=nickname_index
    )

    con = duckdb.connect(str(db_path), read_only=True)
    crosswalk_after_run2 = dict(
        con.execute("SELECT record_key, patient_global_id FROM serving.crosswalk").fetchall()
    )
    demographics_after_run2 = con.execute(
        "SELECT * FROM serving.member_demographics ORDER BY patient_global_id"
    ).fetchall()
    con.close()

    assert crosswalk_after_run1 == crosswalk_after_run2
    assert demographics_after_run1 == demographics_after_run2
    assert summary_2["num_identity_events"] == 0  # nothing changed -- no re-run churn
    assert summary_1["num_golden_records"] == summary_2["num_golden_records"]


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matching_every_field_has_lineage(tmp_path):
    db_path, fs_params, nickname_index = _build_ci_tier_db(tmp_path)
    run_matching(str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index)

    con = duckdb.connect(str(db_path), read_only=True)
    pgid_count = con.execute("SELECT count(*) FROM serving.member_demographics").fetchone()[0]
    lineage_field_count = con.execute(
        "SELECT count(DISTINCT patient_global_id || ':' || field_name) FROM serving.field_lineage"
    ).fetchone()[0]
    distinct_fields = con.execute(
        "SELECT DISTINCT field_name FROM serving.field_lineage ORDER BY 1"
    ).fetchall()
    con.close()

    assert {row[0] for row in distinct_fields} == {
        "first_name",
        "last_name",
        "dob",
        "gender",
        "ssn",
    }
    # every golden record has a lineage row for every one of the 5 fields
    assert lineage_field_count == pgid_count * 5


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matching_never_produces_a_cluster_over_the_size_guard(tmp_path):
    db_path, fs_params, nickname_index = _build_ci_tier_db(tmp_path)
    run_matching(str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index)

    from mdm.config import load_config

    max_cluster_size = load_config()["clustering"]["max_cluster_size"]

    con = duckdb.connect(str(db_path), read_only=True)
    max_size = con.execute("SELECT max(source_record_count) FROM serving.membership").fetchone()[0]
    con.close()

    assert max_size <= max_cluster_size
