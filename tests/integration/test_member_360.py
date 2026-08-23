"""Phase 21 exit criteria: 'member_360 returns a full cross-domain summary for any
patient_global_id, including people with zero records in a given domain (left joins, not
inner).' Proves the left-join behavior and per-domain aggregates against real generated
data, not just that the view compiles."""

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
    return db_path


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_member_360_covers_every_golden_record_including_zero_domain_rows(tmp_path):
    db_path = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    golden_record_count = con.execute(
        "SELECT count(*) FROM serving.member_demographics"
    ).fetchone()[0]
    member_360_count = con.execute("SELECT count(*) FROM serving.member_360").fetchone()[0]
    zero_domain_counts = con.execute(
        "SELECT count(*) FROM serving.member_360 WHERE encounter_count = 0 "
        "OR medical_claim_count = 0 OR pharmacy_claim_count = 0 OR lab_result_count = 0"
    ).fetchone()[0]
    null_counts = con.execute(
        "SELECT count(*) FROM serving.member_360 WHERE encounter_count IS NULL "
        "OR medical_claim_count IS NULL OR pharmacy_claim_count IS NULL "
        "OR lab_result_count IS NULL OR abnormal_lab_count IS NULL "
        "OR active_prescription_count IS NULL"
    ).fetchone()[0]
    con.close()

    # every golden record appears exactly once (left joins from member_demographics, not
    # inner joins against any one domain) -- a person with zero records in every fact
    # domain would otherwise silently vanish from this view.
    assert member_360_count == golden_record_count
    # at ci tier's population, some people genuinely have zero records in at least one
    # domain (not every one of the 6 domains reaches every person) -- if this were 0, the
    # left joins wouldn't be doing anything observable.
    assert zero_domain_counts > 0
    # count columns are declared not_null in schema.yml; this re-checks it directly against
    # real data rather than only trusting the dbt test ran.
    assert null_counts == 0


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_member_360_domain_counts_match_fact_tables(tmp_path):
    db_path = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    checks = {
        "encounter_count": "fct_medical_history",
        "medical_claim_count": "fct_medical_claims",
        "pharmacy_claim_count": "fct_pharmacy_claims",
        "lab_result_count": "fct_lab_results",
    }
    for summary_col, fct_table in checks.items():
        summed = con.execute(f"SELECT sum({summary_col}) FROM serving.member_360").fetchone()[0]
        actual = con.execute(f"SELECT count(*) FROM serving.{fct_table}").fetchone()[0]
        assert summed == actual, f"{summary_col} sum ({summed}) != {fct_table} rows ({actual})"

    # a pharmacy_info record resolves to at most one patient_global_id (Phase 20), so at
    # most one golden record per plan tier value -- no double counting possible here.
    plan_tier_count = con.execute(
        "SELECT count(*) FROM serving.member_360 WHERE pharmacy_plan_tier IS NOT NULL"
    ).fetchone()[0]
    fct_pharmacy_info_count = con.execute(
        "SELECT count(*) FROM serving.fct_pharmacy_info"
    ).fetchone()[0]
    assert plan_tier_count == fct_pharmacy_info_count
    con.close()


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_member_360_active_prescription_count_never_exceeds_total_claims(tmp_path):
    db_path = _build_fully_matched_ci_tier_db(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    violations = con.execute(
        "SELECT count(*) FROM serving.member_360 "
        "WHERE active_prescription_count > pharmacy_claim_count"
    ).fetchone()[0]
    con.close()

    assert violations == 0
