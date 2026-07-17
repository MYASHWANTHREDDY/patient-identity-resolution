import subprocess
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate.py"


def _run_generate(out_dir: Path, workers: int, shard_size: int = 400, tier: str = "ci") -> None:
    subprocess.run(
        [
            sys.executable,
            str(GENERATE_SCRIPT),
            "--tier",
            tier,
            "--seed",
            "42",
            "--workers",
            str(workers),
            "--shard-size",
            str(shard_size),
            "--out-dir",
            str(out_dir),
        ],
        check=True,
        cwd=REPO_ROOT,
    )


def _read_tier_dataset(tier_dir: Path) -> dict:
    dataset = {}
    for vendor_dir in sorted((tier_dir / "raw").iterdir()):
        rows = []
        for part in sorted(vendor_dir.glob("part-*.parquet")):
            rows.extend(pq.read_table(part).to_pylist())
        rows.sort(key=lambda r: r["record_id"])
        dataset[vendor_dir.name] = rows

    gt_rows = []
    for part in sorted((tier_dir / "ground_truth").glob("part-*.parquet")):
        gt_rows.extend(pq.read_table(part).to_pylist())
    gt_rows.sort(key=lambda r: r["record_key"])
    dataset["ground_truth"] = gt_rows
    return dataset


def test_generator_output_is_identical_regardless_of_worker_count(tmp_path):
    out_single = tmp_path / "single"
    out_multi = tmp_path / "multi"

    _run_generate(out_single, workers=1)
    _run_generate(out_multi, workers=3)

    dataset_single = _read_tier_dataset(out_single / "ci")
    dataset_multi = _read_tier_dataset(out_multi / "ci")

    assert dataset_single == dataset_multi


def test_generator_ci_tier_produces_expected_layout_and_all_noise_types(tmp_path):
    out_dir = tmp_path / "out"
    _run_generate(out_dir, workers=2)

    dataset = _read_tier_dataset(out_dir / "ci")
    assert set(dataset) == {"vendor_a", "vendor_b", "vendor_c", "ground_truth"}

    total_vendor_records = sum(len(dataset[v]) for v in ("vendor_a", "vendor_b", "vendor_c"))
    assert total_vendor_records == len(dataset["ground_truth"])
    assert len({row["record_key"] for row in dataset["ground_truth"]}) == len(
        dataset["ground_truth"]
    )

    noise_types = Counter(row["noise_type"] for row in dataset["ground_truth"])
    for noise_type in ("exact", "nickname", "typo_name", "dob_error", "missing_ssn"):
        assert noise_types[noise_type] > 0, f"{noise_type} never occurred"

    vendor_a_row = dataset["vendor_a"][0]
    assert set(vendor_a_row) == {"record_id", "first_name", "last_name", "dob", "gender", "ssn"}
    vendor_b_row = dataset["vendor_b"][0]
    assert set(vendor_b_row) == {
        "record_id",
        "fname",
        "lname",
        "birth_date",
        "sex",
        "social_security_number",
    }
    vendor_c_row = dataset["vendor_c"][0]
    assert set(vendor_c_row) == {
        "record_id",
        "given_name",
        "surname",
        "date_of_birth",
        "Gender",
        "member_id",
    }
