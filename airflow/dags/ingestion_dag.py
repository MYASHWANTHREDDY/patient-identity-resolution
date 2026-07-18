"""GCS Parquet -> raw_standard via BigQuery load jobs (PROJECT_CONSTITUTION.md #15).
One task per vendor, parallel. Idempotent: WRITE_TRUNCATE, same as scripts/load_bigquery.py's
`bq load --replace` (P7 -- safe to re-run). BigQuery load jobs are free.

Orchestration only -- GCSToBigQueryOperator IS the implementation here (a declarative
operator, not custom Python), so there's no `mdm.*` logic to import (PROJECT_CONSTITUTION.md
anti-patterns: "Logic inside DAG files").

Requires scripts/generate.py + scripts/upload_to_gcs.py to have already populated the bucket
for MDM_TIER (see Makefile's `upload-gcs` target) -- this DAG only loads what's already there.

Manually triggered (schedule=None), not on a timer: an accidentally-scheduled DAG that
resubmits load jobs (and, downstream, Dataproc batches) on a timer is exactly the kind of
surprise cost this project's cost discipline (P10) exists to avoid.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.models.dag import DAG
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator

VENDOR_TABLES = ("vendor_a", "vendor_b", "vendor_c")

PROJECT = os.environ["GCP_PROJECT"]
BUCKET = os.environ["GCS_BUCKET"]
TIER = os.environ.get("MDM_TIER", "dev")

with DAG(
    dag_id="ingestion_dag",
    description="GCS Parquet -> raw_standard via BigQuery load jobs, one task per vendor",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["mdm", "ingestion"],
) as dag:
    for vendor_table in VENDOR_TABLES:
        GCSToBigQueryOperator(
            task_id=f"load_{vendor_table}",
            bucket=BUCKET,
            source_objects=[f"{TIER}/raw/{vendor_table}/part-*.parquet"],
            destination_project_dataset_table=f"{PROJECT}.raw_standard.{vendor_table}",
            source_format="PARQUET",
            write_disposition="WRITE_TRUNCATE",
            create_disposition="CREATE_IF_NEEDED",
        )
