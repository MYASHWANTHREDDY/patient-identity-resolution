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


def _generate_and_load(tmp_path: Path) -> Path:
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
    return db_path


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


def _build_ci_tier_db_with_matchpath_resolution(tmp_path: Path) -> Path:
    """Mirrors the real two-phase flow (data -> dbt-build-pre -> estimate-params -> match
    -> run_matchpath_matching -> dbt-build), condensed for tests. Phase 20's serving.fct_*
    tables need serving.matchpath_resolution to exist (written by run_matchpath_matching),
    so the second dbt pass has to happen after both matching steps, not just the core one."""
    db_path = _generate_and_load(tmp_path)

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

    run_matching(str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index)
    run_matchpath_matching(str(db_path), fs_params=fs_params, nickname_index=nickname_index)

    _dbt_build(db_path, profiles_dir, exclude_serving=False)
    return db_path


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matchpath_matching_resolves_against_ground_truth(tmp_path):
    """Real precision/recall against synthetic ground truth, not asserted numbers (P3) --
    loose bounds since this is one small seed-42 ci-tier sample; docs/results.md carries
    the precisely measured figures at dev tier."""
    db_path = _build_ci_tier_db_with_matchpath_resolution(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    mp_gt = con.execute(
        "SELECT record_key, true_identity_id FROM ground_truth.matchpath_ground_truth"
    ).df()
    core_gt = dict(
        con.execute(
            "SELECT record_key, true_identity_id FROM ground_truth.ground_truth"
        ).fetchall()
    )
    resolution = con.execute(
        "SELECT record_key, matched_core_record_key FROM serving.matchpath_resolution"
    ).df()
    con.close()

    mp_key_to_identity = dict(zip(mp_gt["record_key"], mp_gt["true_identity_id"], strict=False))

    tp = fp = 0
    for row in resolution.itertuples():
        true_identity = mp_key_to_identity[row.record_key]
        matched_identity = core_gt.get(row.matched_core_record_key)
        if matched_identity == true_identity:
            tp += 1
        else:
            fp += 1

    recall = tp / len(mp_gt)
    precision = tp / (tp + fp) if (tp + fp) else 0.0

    assert precision >= 0.95
    assert recall >= 0.85


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matchpath_matching_never_resolves_to_wrong_pgid_via_crosswalk(tmp_path):
    """Every resolved patient_global_id must actually correspond to matched_core_record_key
    in serving.crosswalk -- catches a join bug that would silently attach a match-path
    record to an unrelated identity."""
    db_path = _build_ci_tier_db_with_matchpath_resolution(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    mismatches = con.execute(
        "SELECT r.record_key FROM serving.matchpath_resolution r "
        "JOIN serving.crosswalk c ON c.record_key = r.matched_core_record_key "
        "WHERE c.patient_global_id != r.patient_global_id"
    ).fetchall()
    con.close()

    assert mismatches == []


def test_run_matchpath_matching_requires_core_crosswalk_first(tmp_path):
    """serving.crosswalk must exist before match-path resolution runs -- a match-path
    record resolves *against* the core population, so running this first is meaningless
    (docs/domain-linking-strategy.md). Doesn't need dbt: the crosswalk check happens before
    this function ever touches a conformance table."""
    db_path = _generate_and_load(tmp_path)

    with pytest.raises(RuntimeError, match="serving.crosswalk"):
        run_matchpath_matching(str(db_path))


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_run_matchpath_matching_fct_tables_stay_within_conformance_bounds(tmp_path):
    db_path = _build_ci_tier_db_with_matchpath_resolution(tmp_path)

    con = duckdb.connect(str(db_path), read_only=True)
    fct_pharmacy_count = con.execute("SELECT count(*) FROM serving.fct_pharmacy_info").fetchone()[
        0
    ]
    fct_lab_count = con.execute("SELECT count(*) FROM serving.fct_lab_results").fetchone()[0]
    pharmacy_conformance_count = con.execute(
        "SELECT count(*) FROM conformance.pharmacy_info_normalized"
    ).fetchone()[0]
    lab_results_conformance_count = con.execute(
        "SELECT count(*) FROM conformance.lab_results_normalized"
    ).fetchone()[0]
    con.close()

    assert 0 < fct_pharmacy_count <= pharmacy_conformance_count
    assert 0 < fct_lab_count <= lab_results_conformance_count
