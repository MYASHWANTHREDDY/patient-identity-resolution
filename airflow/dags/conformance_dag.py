"""`dbt run` staging + conformance + blocking models, then `dbt test` (PROJECT_CONSTITUTION.md
#15). Excludes serving/snap_member_demographics -- those dbt sources are tables
scripts/run_matching_bigquery.py writes in dedup_dag, which hasn't run yet at this point in
the pipeline (see docs/design-decisions.md, "two-phase dbt flow", the same reason
`make dbt-build-prod` excludes them locally).

Two tasks, not one `dbt build`, so a genuine data/model bug (run failure) and a test
assertion failure show up as distinct, independently-retryable Airflow task states rather
than one undifferentiated failure.

Requires ingestion_dag to have already populated raw_standard for MDM_TIER.
"""

from __future__ import annotations

import os
from datetime import datetime

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator

# dbt lives in its own venv, isolated from Airflow's own site-packages (click version
# conflict -- see airflow/Dockerfile and docs/design-decisions.md, Phase 13).
DBT_BIN = "/home/airflow/dbt-venv/bin/dbt"
DBT_SELECT_ARGS = "--exclude path:models/serving snap_member_demographics"
DBT_ENV = {
    "DBT_PROFILES_DIR": "/opt/airflow/dbt",
    "GCP_PROJECT": os.environ["GCP_PROJECT"],
    "GCP_REGION": os.environ.get("GCP_REGION", "us-central1"),
}

with DAG(
    dag_id="conformance_dag",
    description="dbt run + dbt test: staging, conformance, blocking models against BigQuery",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["mdm", "conformance", "dbt"],
) as dag:
    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=f"cd /opt/airflow/dbt && {DBT_BIN} run --target prod {DBT_SELECT_ARGS}",
        env=DBT_ENV,
        append_env=True,
    )

    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=f"cd /opt/airflow/dbt && {DBT_BIN} test --target prod {DBT_SELECT_ARGS}",
        env=DBT_ENV,
        append_env=True,
    )

    dbt_run >> dbt_test
