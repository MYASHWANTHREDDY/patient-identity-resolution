"""Local backend: the DuckDB equivalent of a BigQuery load job (P8).

Loads a tier's generated Parquet straight into `raw_standard` with no transform -- Layer 1
stays an auditable record of exactly what arrived. dbt takes over from there.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

VENDOR_TABLES = ("vendor_a", "vendor_b", "vendor_c")


def load_tier_to_duckdb(tier_dir: Path, db_path: Path) -> dict[str, int]:
    if not tier_dir.exists():
        raise FileNotFoundError(
            f"No generated data at {tier_dir} -- run scripts/generate.py first"
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    counts: dict[str, int] = {}
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw_standard")
        con.execute("CREATE SCHEMA IF NOT EXISTS ground_truth")

        for vendor_table in VENDOR_TABLES:
            parquet_glob = (tier_dir / "raw" / vendor_table / "part-*.parquet").as_posix()
            con.execute(
                f"CREATE OR REPLACE TABLE raw_standard.{vendor_table} AS "
                f"SELECT * FROM read_parquet('{parquet_glob}')"
            )
            counts[vendor_table] = con.execute(
                f"SELECT count(*) FROM raw_standard.{vendor_table}"
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
