#!/usr/bin/env python
"""Verify DuckDB and BigQuery produce identical dbt output on the same tier's input
(PROJECT_CONSTITUTION.md #10, Phase 11 exit criterion: "DuckDB and BigQuery produce
identical results on the same 50k input"). Requires both to have already been built
(dbt build against `dev` target locally, and `prod` target against the same tier's data
loaded into BigQuery raw_standard).

    python scripts/verify_tier_parity.py --tier dev --project patient-dedup-mdm
"""

from __future__ import annotations

import argparse

import duckdb
import pandas as pd
from google.cloud import bigquery

from mdm.config import REPO_ROOT, VALID_TIERS

# normalized_at is a build-time timestamp, not pipeline output -- excluded from comparison
PATIENT_NORMALIZED_COLUMNS = [
    "source_vendor",
    "source_record_id",
    "record_key",
    "first_name",
    "last_name",
    "dob",
    "gender",
    "ssn",
    "first_name_phonetic",
    "last_name_phonetic",
    "dob_year",
    "dob_decade",
]
CANDIDATE_PAIRS_COLUMNS = ["record_key_a", "record_key_b", "blocking_pass", "block_key"]


def _load_duckdb(db_path: str, table: str, columns: list[str]) -> pd.DataFrame:
    con = duckdb.connect(db_path, read_only=True)
    try:
        cols = ", ".join(columns)
        return con.execute(f"SELECT {cols} FROM {table}").df()
    finally:
        con.close()


def _load_bigquery(project: str, table: str, columns: list[str]) -> pd.DataFrame:
    client = bigquery.Client(project=project)
    cols = ", ".join(columns)
    return client.query(f"SELECT {cols} FROM `{project}.{table}`").to_dataframe()


def _normalize_for_comparison(df: pd.DataFrame, sort_columns: list[str]) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        # BigQuery returns DATE columns as datetime.date already; DuckDB the same -- but
        # dtype backing (e.g. object vs. a pandas date extension type) can still differ,
        # so normalize to string for a robust, backend-agnostic comparison.
        df[col] = df[col].astype(str)
    return df.sort_values(sort_columns).reset_index(drop=True)


def compare_table(
    duckdb_path: str,
    bq_project: str,
    duckdb_table: str,
    bq_table: str,
    columns: list[str],
    sort_columns: list[str],
) -> dict:
    duckdb_df = _normalize_for_comparison(
        _load_duckdb(duckdb_path, duckdb_table, columns), sort_columns
    )
    bigquery_df = _normalize_for_comparison(
        _load_bigquery(bq_project, bq_table, columns), sort_columns
    )

    matches = duckdb_df.equals(bigquery_df)
    return {
        "table": duckdb_table,
        "duckdb_rows": len(duckdb_df),
        "bigquery_rows": len(bigquery_df),
        "matches": matches,
    }


def main(argv: list[str] | None = None) -> list[dict]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", choices=VALID_TIERS, default="dev")
    parser.add_argument("--project", required=True)
    parser.add_argument("--db-path", type=str, default=None)
    args = parser.parse_args(argv)

    db_path = args.db_path or str(REPO_ROOT / "data" / args.tier / "mdm.duckdb")

    results = [
        compare_table(
            db_path,
            args.project,
            "conformance.patient_normalized",
            "conformance.patient_normalized",
            PATIENT_NORMALIZED_COLUMNS,
            ["record_key"],
        ),
        compare_table(
            db_path,
            args.project,
            "matching.candidate_pairs",
            "matching.candidate_pairs",
            CANDIDATE_PAIRS_COLUMNS,
            ["record_key_a", "record_key_b", "blocking_pass"],
        ),
    ]

    for result in results:
        status = "MATCH" if result["matches"] else "MISMATCH"
        print(
            f"[{status}] {result['table']}: duckdb={result['duckdb_rows']} rows, "
            f"bigquery={result['bigquery_rows']} rows"
        )

    if not all(r["matches"] for r in results):
        raise SystemExit(1)

    return results


if __name__ == "__main__":
    main()
