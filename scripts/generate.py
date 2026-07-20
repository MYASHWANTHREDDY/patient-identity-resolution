#!/usr/bin/env python
"""Tiered synthetic patient data generator.

    python scripts/generate.py --tier {ci,dev,scale} --seed 42

Emits three vendor Parquet trees plus a labeled ground_truth.parquet under
`data/{tier}/...`, mirroring the GCS layout in PROJECT_CONSTITUTION.md #9. Same seed,
same tier -> identical output regardless of --workers (P6, P7).
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from mdm.config import REPO_ROOT, VALID_TIERS, resolve_tier
from mdm.generator.facts import (
    MEDICAL_CLAIMS_SCHEMA,
    MEDICAL_CLAIMS_VENDORS,
    MEDICAL_HISTORY_SCHEMA,
    MEDICAL_HISTORY_VENDORS,
    PHARMACY_CLAIMS_SCHEMA,
    PHARMACY_CLAIMS_VENDORS,
    VENDOR_ID_MAP_SCHEMA,
)
from mdm.generator.shard import CHUNK_SIZE, generate_shard_task, shard_ranges
from mdm.generator.vendors import GROUND_TRUTH_SCHEMA, VENDOR_SCHEMAS, VENDORS
from mdm.reference_codes import load_hcpcs, load_icd10cm, load_ndc

DEFAULT_NICKNAME_TABLE_PATH = REPO_ROOT / "config" / "nicknames.yml"
DEFAULT_OUT_DIR = REPO_ROOT / "data"

FACT_TABLE_SPECS = {
    "medical_history": (MEDICAL_HISTORY_VENDORS, MEDICAL_HISTORY_SCHEMA),
    "medical_claims": (MEDICAL_CLAIMS_VENDORS, MEDICAL_CLAIMS_SCHEMA),
    "pharmacy_claims": (PHARMACY_CLAIMS_VENDORS, PHARMACY_CLAIMS_SCHEMA),
}


def load_nickname_table(path: Path) -> dict[str, list[str]]:
    with path.open("r", encoding="utf-8") as f:
        table = yaml.safe_load(f) or {}
    return table


def write_parquet(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        table = pa.Table.from_pylist(rows, schema=schema)
    else:
        empty_columns = {field.name: pa.array([], type=field.type) for field in schema}
        table = pa.table(empty_columns, schema=schema)
    pq.write_table(table, path)


def run(
    tier_name: str,
    seed: int,
    workers: int,
    out_dir: Path,
    chunk_size: int,
    nickname_table_path: Path,
    *,
    with_facts: bool = True,
) -> dict:
    tier = resolve_tier(tier_name)
    nickname_table = load_nickname_table(nickname_table_path)
    ranges = shard_ranges(tier.num_identities, chunk_size)

    # Reference code *lists* (not the full code->description dicts) -- passed through the
    # task tuple to every shard worker the same way nickname_table already is (P8: one
    # generation path, not a special case for fact domains). Loaded once here, not once per
    # shard, since ci/dev tier only ever needs a handful of shards -- see
    # docs/design-decisions.md if this ever needs to scale to many more shards.
    icd10cm_codes = list(load_icd10cm()) if with_facts else None
    hcpcs_codes = list(load_hcpcs()) if with_facts else None
    ndc_codes = list(load_ndc()) if with_facts else None

    tasks = [
        (i, start, end, seed, nickname_table, icd10cm_codes, hcpcs_codes, ndc_codes)
        for i, (start, end) in enumerate(ranges)
    ]

    vendor_shards: dict[str, list[list[dict]]] = {v: [None] * len(ranges) for v in VENDORS}
    ground_truth_shards: list[list[dict] | None] = [None] * len(ranges)
    fact_shards: dict[str, dict[str, list[list[dict]]]] = {
        domain: {v: [None] * len(ranges) for v in vendors}
        for domain, (vendors, _schema) in FACT_TABLE_SPECS.items()
    }
    vendor_id_map_shards: list[list[dict] | None] = [None] * len(ranges)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        for shard_index, (vendor_rows, gt_rows, fact_rows) in enumerate(
            executor.map(generate_shard_task, tasks)
        ):
            for vendor in VENDORS:
                vendor_shards[vendor][shard_index] = vendor_rows[vendor]
            ground_truth_shards[shard_index] = gt_rows
            for domain, (vendors, _schema) in FACT_TABLE_SPECS.items():
                for vendor in vendors:
                    fact_shards[domain][vendor][shard_index] = fact_rows[domain][vendor]
            vendor_id_map_shards[shard_index] = fact_rows["vendor_id_map"]

    out_root = out_dir / tier_name
    for vendor in VENDORS:
        vendor_dir = out_root / "raw" / vendor.lower()
        for shard_index, rows in enumerate(vendor_shards[vendor]):
            write_parquet(
                vendor_dir / f"part-{shard_index:05d}.parquet", rows, VENDOR_SCHEMAS[vendor]
            )

    gt_dir = out_root / "ground_truth"
    for shard_index, rows in enumerate(ground_truth_shards):
        write_parquet(gt_dir / f"part-{shard_index:05d}.parquet", rows, GROUND_TRUTH_SCHEMA)

    fact_counts: dict[str, dict[str, int]] = {}
    if with_facts:
        for domain, (vendors, schema) in FACT_TABLE_SPECS.items():
            fact_counts[domain] = {}
            for vendor in vendors:
                domain_dir = out_root / "raw" / f"{vendor.lower()}_{domain}"
                shards = fact_shards[domain][vendor]
                for shard_index, rows in enumerate(shards):
                    write_parquet(domain_dir / f"part-{shard_index:05d}.parquet", rows, schema)
                fact_counts[domain][vendor] = sum(len(r) for r in shards)

        vid_dir = out_root / "raw" / "vendor_id_map"
        for shard_index, rows in enumerate(vendor_id_map_shards):
            write_parquet(vid_dir / f"part-{shard_index:05d}.parquet", rows, VENDOR_ID_MAP_SCHEMA)
        fact_counts["vendor_id_map"] = {
            "total": sum(len(r) for r in vendor_id_map_shards)
        }

    record_counts = {vendor: sum(len(r) for r in rows) for vendor, rows in vendor_shards.items()}
    return {
        "tier": tier_name,
        "num_identities": tier.num_identities,
        "shards": len(ranges),
        "record_counts": record_counts,
        "fact_counts": fact_counts,
        "total_records": sum(record_counts.values()),
    }


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--shard-size", type=int, default=CHUNK_SIZE)
    parser.add_argument("--nicknames", type=Path, default=DEFAULT_NICKNAME_TABLE_PATH)
    parser.add_argument(
        "--no-facts",
        action="store_true",
        help="Skip medical_history/medical_claims/pharmacy_claims generation (Phase 19) -- "
        "member/eligibility data only, matching this script's pre-Phase-19 behavior.",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()
    summary = run(
        args.tier,
        args.seed,
        args.workers,
        args.out_dir,
        args.shard_size,
        args.nicknames,
        with_facts=not args.no_facts,
    )
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)

    print(
        f"tier={summary['tier']} identities={summary['num_identities']} "
        f"records={summary['total_records']} shards={summary['shards']} "
        f"counts={summary['record_counts']} fact_counts={summary['fact_counts']} "
        f"elapsed={summary['elapsed_seconds']}s"
    )
    return summary


if __name__ == "__main__":
    main()
