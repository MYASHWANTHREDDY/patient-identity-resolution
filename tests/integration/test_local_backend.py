import subprocess
import sys
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from mdm.backends.local import load_tier_to_duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


def _generate(out_dir: Path) -> Path:
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
            str(out_dir),
        ],
        check=True,
        cwd=REPO_ROOT,
    )
    return out_dir / "ci"


def test_load_tier_to_duckdb_row_counts_match_parquet(tmp_path):
    tier_dir = _generate(tmp_path / "data")
    db_path = tmp_path / "mdm.duckdb"

    counts = load_tier_to_duckdb(tier_dir, db_path)

    for vendor_table in ("vendor_a", "vendor_b", "vendor_c"):
        parts = list((tier_dir / "raw" / vendor_table).glob("part-*.parquet"))
        expected = sum(pq.read_table(p).num_rows for p in parts)
        assert counts[vendor_table] == expected

    gt_parts = list((tier_dir / "ground_truth").glob("part-*.parquet"))
    expected_gt = sum(pq.read_table(p).num_rows for p in gt_parts)
    assert counts["ground_truth"] == expected_gt


def test_load_tier_to_duckdb_creates_queryable_schemas(tmp_path):
    tier_dir = _generate(tmp_path / "data")
    db_path = tmp_path / "mdm.duckdb"
    load_tier_to_duckdb(tier_dir, db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(
            "SELECT first_name, last_name, dob, gender, ssn FROM raw_standard.vendor_a LIMIT 1"
        ).fetchone()
        assert row is not None
        gt_row = con.execute(
            "SELECT record_key, true_identity_id, noise_type FROM ground_truth.ground_truth LIMIT 1"
        ).fetchone()
        assert gt_row is not None
    finally:
        con.close()


def test_load_tier_to_duckdb_missing_tier_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_tier_to_duckdb(tmp_path / "does_not_exist", tmp_path / "out.duckdb")


def test_load_tier_to_duckdb_loads_matchpath_tables_separately_from_core_ground_truth(tmp_path):
    """Phase 20: pharmacy_info/lab_identity/lab_results land in raw_standard, and
    matchpath_ground_truth lands in its own ground_truth.matchpath_ground_truth table --
    never unioned into ground_truth.ground_truth, since mixing them would introduce
    spurious "true pairs" between core and match-path records (PROJECT_CONSTITUTION.md
    Phase 20)."""
    tier_dir = _generate(tmp_path / "data")
    db_path = tmp_path / "mdm.duckdb"

    counts = load_tier_to_duckdb(tier_dir, db_path)

    for matchpath_table in ("pharmacy_info", "lab_identity", "lab_results"):
        parts = list((tier_dir / "raw" / matchpath_table).glob("part-*.parquet"))
        expected = sum(pq.read_table(p).num_rows for p in parts)
        assert counts[matchpath_table] == expected

    gt_parts = list((tier_dir / "matchpath_ground_truth").glob("part-*.parquet"))
    expected_matchpath_gt = sum(pq.read_table(p).num_rows for p in gt_parts)
    assert counts["matchpath_ground_truth"] == expected_matchpath_gt

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        core_keys = {
            r[0] for r in con.execute("SELECT record_key FROM ground_truth.ground_truth").fetchall()
        }
        matchpath_keys = {
            r[0]
            for r in con.execute(
                "SELECT record_key FROM ground_truth.matchpath_ground_truth"
            ).fetchall()
        }
        assert core_keys.isdisjoint(matchpath_keys)

        lab_result_row = con.execute(
            "SELECT source_record_id, test_code FROM raw_standard.lab_results LIMIT 1"
        ).fetchone()
        assert lab_result_row is not None
    finally:
        con.close()
