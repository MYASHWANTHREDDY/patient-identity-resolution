"""Phase 22 exit criteria: 'a downstream caller with no knowledge of source vendors,
domains, or table structure can resolve a new record to an ID and fetch that person's full
cross-domain profile through two API calls.'"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml
from fastapi.testclient import TestClient

from mdm.api import create_app
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

    run_matching(str(db_path), run_id="run1", fs_params=fs_params, nickname_index=nickname_index)
    run_matchpath_matching(str(db_path), fs_params=fs_params, nickname_index=nickname_index)

    _dbt_build(db_path, profiles_dir, exclude_serving=False)
    return db_path


@pytest.fixture
def client(tmp_path):
    db_path = _build_fully_matched_ci_tier_db(tmp_path)
    app = create_app(db_path)
    return TestClient(app), db_path


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_resolve_and_fetch_two_call_contract_for_an_existing_person(client):
    test_client, db_path = client

    con = duckdb.connect(str(db_path), read_only=True)
    existing_pgid, first_name, last_name, dob, gender = con.execute(
        "SELECT d.patient_global_id, d.first_name, d.last_name, d.dob, d.gender "
        "FROM serving.member_demographics d LIMIT 1"
    ).fetchone()
    con.close()

    # Call 1: resolve -- the same demographics should auto-match back to the existing person.
    resolve_response = test_client.post(
        "/resolve",
        json={
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob.isoformat(),
            "gender": gender,
        },
    )
    assert resolve_response.status_code == 200
    resolved = resolve_response.json()
    assert resolved["status"] == "matched"
    assert resolved["patient_global_id"] == existing_pgid

    # Call 2: fetch -- the full cross-domain profile for that same ID.
    profile_response = test_client.get(f"/members/{existing_pgid}")
    assert profile_response.status_code == 200
    profile = profile_response.json()
    assert profile["patient_global_id"] == existing_pgid
    assert profile["summary"]["first_name"] == first_name
    assert set(profile["domains"]) == {
        "medical_history",
        "medical_claims",
        "pharmacy_claims",
        "pharmacy_info",
        "lab_results",
    }
    for domain_payload in profile["domains"].values():
        assert domain_payload["total"] >= 0
        assert domain_payload["returned"] == len(domain_payload["records"])


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_resolve_a_genuinely_new_person_creates_an_identity_visible_immediately(client):
    test_client, _db_path = client

    resolve_response = test_client.post(
        "/resolve",
        json={
            "first_name": "Zzyxwvutsrq",
            "last_name": "Qqpponmlkj",
            "dob": "1901-01-01",
            "gender": "F",
        },
    )
    assert resolve_response.status_code == 200
    resolved = resolve_response.json()
    assert resolved["status"] == "created"
    new_pgid = resolved["patient_global_id"]

    # member_360 is a live view -- the new identity must be queryable immediately, no batch
    # rebuild required (PROJECT_CONSTITUTION.md #9's original Phase 13 decision).
    profile_response = test_client.get(f"/members/{new_pgid}")
    assert profile_response.status_code == 200
    profile = profile_response.json()
    assert profile["summary"]["first_name"] == "ZZYXWVUTSRQ"
    assert profile["summary"]["last_name"] == "QQPPONMLKJ"
    for domain_payload in profile["domains"].values():
        assert domain_payload["total"] == 0  # a brand-new person has no fact-table history


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_resolving_the_same_new_person_twice_mints_two_ids(client):
    """Documents a real, deliberate limitation (see resolve_new_record's docstring): an
    API-created identity is written only to the serving layer, never back into
    conformance.patient_normalized, so a second resolve call for the same person can't find
    the first call's record as a blocking candidate -- durability requires the record to
    actually flow through a vendor feed and the next batch run_matching()."""
    test_client, _db_path = client
    payload = {
        "first_name": "Novelperson",
        "last_name": "Neverseen",
        "dob": "1955-06-15",
        "gender": "M",
    }

    first = test_client.post("/resolve", json=payload).json()
    second = test_client.post("/resolve", json=payload).json()

    assert first["status"] == "created"
    assert second["status"] == "created"
    assert first["patient_global_id"] != second["patient_global_id"]


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_get_member_domain_pagination(client):
    test_client, db_path = client

    con = duckdb.connect(str(db_path), read_only=True)
    pgid, total = con.execute(
        "SELECT patient_global_id, medical_claim_count FROM serving.member_360 "
        "WHERE medical_claim_count > 1 ORDER BY medical_claim_count DESC LIMIT 1"
    ).fetchone()
    con.close()

    page_1 = test_client.get(f"/members/{pgid}/medical_claims", params={"limit": 1, "offset": 0})
    page_2 = test_client.get(f"/members/{pgid}/medical_claims", params={"limit": 1, "offset": 1})

    assert page_1.status_code == 200
    body_1 = page_1.json()
    body_2 = page_2.json()
    assert body_1["total"] == total
    assert len(body_1["records"]) == 1
    assert len(body_2["records"]) == 1
    assert body_1["records"] != body_2["records"]


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_get_member_unknown_id_returns_404(client):
    test_client, _db_path = client
    response = test_client.get("/members/PGID999999999999")
    assert response.status_code == 404


@pytest.mark.skipif(not _dbt_available(), reason="dbt not installed")
def test_get_member_unknown_domain_returns_404(client):
    test_client, db_path = client
    con = duckdb.connect(str(db_path), read_only=True)
    pgid = con.execute("SELECT patient_global_id FROM serving.member_360 LIMIT 1").fetchone()[0]
    con.close()

    response = test_client.get(f"/members/{pgid}/not_a_real_domain")
    assert response.status_code == 404
