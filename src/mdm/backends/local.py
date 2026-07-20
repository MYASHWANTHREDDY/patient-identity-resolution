"""Local backend: the DuckDB equivalent of a BigQuery load job (P8).

Loads a tier's generated Parquet straight into `raw_standard` with no transform -- Layer 1
stays an auditable record of exactly what arrived. dbt takes over from there.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

VENDOR_TABLES = ("vendor_a", "vendor_b", "vendor_c")

# Phase 19 fact-domain tables: {vendor}_{domain} per docs/domain-linking-strategy.md's
# matrix, plus the Path B id map. Discovered from whatever raw/ subdirectories actually
# exist rather than hardcoded here too -- a tier generated with --no-facts (member domain
# only) simply won't have these directories, and loading skips them rather than erroring,
# so this one function keeps working for both the pre-Phase-19 and post-Phase-19 case.
FACT_TABLE_PREFIXES = ("vendor_a_", "vendor_b_", "vendor_c_")


def load_tier_to_duckdb(tier_dir: Path, db_path: Path) -> dict[str, int]:
    if not tier_dir.exists():
        raise FileNotFoundError(
            f"No generated data at {tier_dir} -- run scripts/generate.py first"
        )

    raw_dir = tier_dir / "raw"
    fact_tables = sorted(
        p.name
        for p in raw_dir.iterdir()
        if p.is_dir() and (p.name.startswith(FACT_TABLE_PREFIXES) or p.name == "vendor_id_map")
    )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    counts: dict[str, int] = {}
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw_standard")
        con.execute("CREATE SCHEMA IF NOT EXISTS ground_truth")

        for table_name in (*VENDOR_TABLES, *fact_tables):
            parquet_glob = (raw_dir / table_name / "part-*.parquet").as_posix()
            con.execute(
                f"CREATE OR REPLACE TABLE raw_standard.{table_name} AS "
                f"SELECT * FROM read_parquet('{parquet_glob}')"
            )
            counts[table_name] = con.execute(
                f"SELECT count(*) FROM raw_standard.{table_name}"
            ).fetchone()[0]

        gt_glob = (tier_dir / "ground_truth" / "part-*.parquet").as_posix()
        con.execute(
            "CREATE OR REPLACE TABLE ground_truth.ground_truth AS "
            f"SELECT * FROM read_parquet('{gt_glob}')"
        )
        counts["ground_truth"] = con.execute(
            "SELECT count(*) FROM ground_truth.ground_truth"
        ).fetchone()[0]
    finally:
        con.close()

    return counts
