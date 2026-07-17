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
from mdm.generator.shard import CHUNK_SIZE, generate_shard_task, shard_ranges
from mdm.generator.vendors import GROUND_TRUTH_SCHEMA, VENDOR_SCHEMAS, VENDORS

DEFAULT_NICKNAME_TABLE_PATH = REPO_ROOT / "config" / "nicknames.yml"
DEFAULT_OUT_DIR = REPO_ROOT / "data"


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
) -> dict:
    tier = resolve_tier(tier_name)
    nickname_table = load_nickname_table(nickname_table_path)
    ranges = shard_ranges(tier.num_identities, chunk_size)
    tasks = [(i, start, end, seed, nickname_table) for i, (start, end) in enumerate(ranges)]

    vendor_shards: dict[str, list[list[dict]]] = {v: [None] * len(ranges) for v in VENDORS}
    ground_truth_shards: list[list[dict] | None] = [None] * len(ranges)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        for shard_index, (vendor_rows, gt_rows) in enumerate(
            executor.map(generate_shard_task, tasks)
        ):
            for vendor in VENDORS:
                vendor_shards[vendor][shard_index] = vendor_rows[vendor]
            ground_truth_shards[shard_index] = gt_rows

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

    record_counts = {vendor: sum(len(r) for r in rows) for vendor, rows in vendor_shards.items()}
    return {
        "tier": tier_name,
        "num_identities": tier.num_identities,
        "shards": len(ranges),
        "record_counts": record_counts,
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
    args = parser.parse_args(argv)

    started = time.monotonic()
    summary = run(args.tier, args.seed, args.workers, args.out_dir, args.shard_size, args.nicknames)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)

    print(
        f"tier={summary['tier']} identities={summary['num_identities']} "
        f"records={summary['total_records']} shards={summary['shards']} "
        f"counts={summary['record_counts']} elapsed={summary['elapsed_seconds']}s"
    )
    return summary


if __name__ == "__main__":
    main()
