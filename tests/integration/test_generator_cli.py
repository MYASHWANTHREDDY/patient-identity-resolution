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


MEMBER_VENDOR_DIRS = ("vendor_a", "vendor_b", "vendor_c")

# Phase 19 fact-domain raw directories -- {vendor}_{domain}, plus the Path B id map. Each
# has its own sort key, unlike the member domain's uniform "record_id" (see
# docs/domain-linking-strategy.md for why the schemas differ by domain, not by vendor).
FACT_DIR_SORT_KEYS = {
    "vendor_a_medical_history": "source_encounter_id",
    "vendor_c_medical_history": "source_encounter_id",
    "vendor_a_medical_claims": "source_claim_id",
    "vendor_b_medical_claims": "source_claim_id",
    "vendor_a_pharmacy_claims": "source_rx_id",
    "vendor_c_pharmacy_claims": "source_rx_id",
    "vendor_id_map": "pbm_member_id",
}


def _read_tier_dataset(tier_dir: Path) -> dict:
    """Member domain only (vendor_a/b/c) -- all share the "record_id" sort key this
    function assumes. Fact-domain output has its own reader, _read_fact_dataset, since its
    per-domain schemas don't share one common key the way the member domain's do."""
    dataset = {}
    for vendor_name in MEMBER_VENDOR_DIRS:
        rows = []
        for part in sorted((tier_dir / "raw" / vendor_name).glob("part-*.parquet")):
            rows.extend(pq.read_table(part).to_pylist())
        rows.sort(key=lambda r: r["record_id"])
        dataset[vendor_name] = rows

    gt_rows = []
    for part in sorted((tier_dir / "ground_truth").glob("part-*.parquet")):
        gt_rows.extend(pq.read_table(part).to_pylist())
    gt_rows.sort(key=lambda r: r["record_key"])
    dataset["ground_truth"] = gt_rows
    return dataset


def _read_fact_dataset(tier_dir: Path) -> dict:
    dataset = {}
    for dir_name, sort_key in FACT_DIR_SORT_KEYS.items():
        rows = []
        for part in sorted((tier_dir / "raw" / dir_name).glob("part-*.parquet")):
            rows.extend(pq.read_table(part).to_pylist())
        rows.sort(key=lambda r: r[sort_key])
        dataset[dir_name] = rows
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


def test_generator_fact_domains_identical_regardless_of_worker_count(tmp_path):
    """Phase 19: fact-domain generation uses its own independently-seeded rng
    (src/mdm/generator/shard.py's facts_rng) specifically so it doesn't perturb the member
    domain -- this is the mirror check, confirming fact-domain output is itself
    reproducible and worker-count-independent the same way the member domain already is."""
    out_single = tmp_path / "single"
    out_multi = tmp_path / "multi"

    _run_generate(out_single, workers=1)
    _run_generate(out_multi, workers=3)

    facts_single = _read_fact_dataset(out_single / "ci")
    facts_multi = _read_fact_dataset(out_multi / "ci")

    assert facts_single == facts_multi
    assert sum(len(rows) for rows in facts_single.values()) > 0


def test_generator_fact_domains_reference_real_codes(tmp_path):
    """Every diagnosis/procedure/drug code in generated fact records must come from the
    real reference tables fetched in Phase 18 -- never invented (P3)."""
    from mdm.reference_codes import load_hcpcs, load_icd10cm, load_ndc

    out_dir = tmp_path / "out"
    _run_generate(out_dir, workers=2)
    facts = _read_fact_dataset(out_dir / "ci")

    icd10cm_codes = set(load_icd10cm())
    hcpcs_codes = set(load_hcpcs())
    ndc_codes = set(load_ndc())

    history_rows = facts["vendor_a_medical_history"] + facts["vendor_c_medical_history"]
    assert history_rows
    assert all(row["condition_code"] in icd10cm_codes for row in history_rows)

    claims_rows = facts["vendor_a_medical_claims"] + facts["vendor_b_medical_claims"]
    assert claims_rows
    assert all(row["diagnosis_code"] in icd10cm_codes for row in claims_rows)
    assert all(row["procedure_code"] in hcpcs_codes for row in claims_rows)

    rx_rows = facts["vendor_a_pharmacy_claims"] + facts["vendor_c_pharmacy_claims"]
    assert rx_rows
    assert all(row["ndc_code"] in ndc_codes for row in rx_rows)

    # Path B: every pharmacy claim's pbm_member_id must appear in vendor_id_map, and map
    # back to a real member_id -- otherwise fct_pharmacy_claims.sql's join would silently
    # drop it (exactly what assert_conservation_pharmacy_claims_no_records_lost.sql checks
    # at the dbt level; this is the same invariant, checked at the generator level).
    mapped_ids = {row["pbm_member_id"] for row in facts["vendor_id_map"]}
    assert all(row["pbm_member_id"] in mapped_ids for row in rx_rows)
