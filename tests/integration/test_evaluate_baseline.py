import subprocess
import sys
from pathlib import Path

from mdm.backends.local import load_tier_to_duckdb
from mdm.evaluate import run_baseline_evaluation

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


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


def test_deterministic_baseline_recovers_exact_and_missing_ssn_perfectly(tmp_path):
    db_path = _generate_and_load(tmp_path)

    # conformance.patient_normalized doesn't exist without dbt -- build it by hand here so
    # this test doesn't depend on dbt being installed (the dbt-build path is covered by
    # test_dbt_conformance.py).
    import duckdb

    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE SCHEMA IF NOT EXISTS conformance;
        CREATE TABLE conformance.patient_normalized AS
        SELECT 'VENDOR_A' AS source_vendor, record_id AS source_record_id,
               'VENDOR_A:' || record_id AS record_key,
               upper(first_name) AS first_name, upper(last_name) AS last_name,
               dob::date AS dob, gender, nullif(ssn, '') AS ssn
        FROM raw_standard.vendor_a
        UNION ALL
        SELECT 'VENDOR_B', record_id, 'VENDOR_B:' || record_id,
               upper(fname), upper(lname),
               strptime(birth_date, '%m/%d/%Y')::date, sex,
               nullif(social_security_number, '')
        FROM raw_standard.vendor_b
        UNION ALL
        SELECT 'VENDOR_C', record_id, 'VENDOR_C:' || record_id,
               upper(given_name), upper(surname),
               strptime(date_of_birth, '%d-%b-%Y')::date, gender,
               NULL
        FROM raw_standard.vendor_c
        """
    )
    con.close()

    result = run_baseline_evaluation("ci", db_path)

    assert result["metrics"]["precision"] > 0.99
    by_noise = result["recall_by_noise_type"]
    assert by_noise["exact"]["recall"] == 1.0
    assert by_noise["missing_ssn"]["recall"] == 1.0
    # corrupted fields defeat both deterministic rules unless SSN happens to survive
    # untouched on a non-Vendor_C pair (~1/3 of the time, see docs/design-decisions.md)
    assert 0.15 < by_noise["typo_name"]["recall"] < 0.55
