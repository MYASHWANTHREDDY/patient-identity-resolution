"""Dataproc scoring -> triage -> clustering -> crosswalk -> survivorship -> dbt run serving
-> dbt snapshot -> quality gates (PROJECT_CONSTITUTION.md #15). This is the phase's main
cost driver -- Dataproc Serverless batches, not Airflow itself (which is free, local Docker).

Scoring (spark_jobs/score_pairs.py) and clustering (spark_jobs/cluster_identities.py) run on
Dataproc Serverless because they're pair-count-scaled; both already bake in triage's
AUTO_MATCH threshold as their edge filter (see spark_jobs/cluster_identities.py). Crosswalk
resolution and golden-record survivorship (scripts/run_matching_bigquery.py) run as a plain
Python task, not on Dataproc, because they're record-count-scaled, not pair-count-scaled --
running Spark for a step that's cheap in pure Python would just be paying for compute that
does nothing (PROJECT_CONSTITUTION.md anti-patterns: "Forcing everything into dbt" applies
symmetrically to forcing everything onto Spark).

Retries: 2 with backoff on the Dataproc batches (a transient submission hiccup shouldn't
fail the whole run). Zero on quality gates -- a quality-gate failure is real information
about the data, not a transient fault worth silently retrying.

Requires conformance_dag to have already built conformance.patient_normalized and
matching.candidate_pairs, and config/fs_params.yml to exist (scripts/estimate_fs_params.py).
The dependency zips/scripts this DAG references on GCS come from `make upload-spark-deps`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.models.dag import DAG
from airflow.operators.bash import BashOperator
from airflow.providers.google.cloud.operators.dataproc import DataprocCreateBatchOperator

# mdm.config only needs stdlib + PyYAML (both present in this image); mdm.pipeline pulls in
# pandas, which it does not have -- so the scale-tier cutoff is read from config here rather
# than via load_thresholds().
from mdm.config import load_config

SCALE_THRESHOLDS = load_config()["thresholds"]["scale"]

PROJECT = os.environ["GCP_PROJECT"]
REGION = os.environ.get("GCP_REGION", "us-central1")
BUCKET = os.environ["GCS_BUCKET"]
SERVICE_ACCOUNT = f"mdm-pipeline@{PROJECT}.iam.gserviceaccount.com"
DEPS_PREFIX = f"gs://{BUCKET}/dependencies"

# dbt lives in its own venv, isolated from Airflow's own site-packages (click version
# conflict -- see airflow/Dockerfile and docs/design-decisions.md, Phase 13).
DBT_BIN = "/home/airflow/dbt-venv/bin/dbt"
DBT_ENV = {
    "DBT_PROFILES_DIR": "/opt/airflow/dbt",
    "GCP_PROJECT": PROJECT,
    "GCP_REGION": REGION,
}
SCRIPT_ENV = {"GCP_PROJECT": PROJECT}

DATAPROC_RETRY_KWARGS = {
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
    "retry_exponential_backoff": True,
}


def _pyspark_batch(main_file: str, extra_args: list[str]) -> dict:
    return {
        "pyspark_batch": {
            "main_python_file_uri": f"{DEPS_PREFIX}/{main_file}",
            "python_file_uris": [f"{DEPS_PREFIX}/mdm.zip", f"{DEPS_PREFIX}/rapidfuzz.zip"],
            "file_uris": [f"{DEPS_PREFIX}/fs_params.yml", f"{DEPS_PREFIX}/nicknames.yml"],
            "args": extra_args,
        },
        "environment_config": {
            "execution_config": {"service_account": SERVICE_ACCOUNT},
        },
    }


with DAG(
    dag_id="dedup_dag",
    description=(
        "Dataproc scoring/clustering -> crosswalk/survivorship -> dbt serving -> "
        "snapshot -> quality gates"
    ),
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["mdm", "dedup", "dataproc"],
) as dag:
    score_pairs = DataprocCreateBatchOperator(
        task_id="dataproc_score_pairs",
        project_id=PROJECT,
        region=REGION,
        batch_id="score-pairs-{{ ts_nodash | lower }}",
        batch=_pyspark_batch(
            "score_pairs.py",
            ["--project", PROJECT, "--bq-temp-bucket", BUCKET],
        ),
        **DATAPROC_RETRY_KWARGS,
    )

    cluster_identities = DataprocCreateBatchOperator(
        task_id="dataproc_cluster_identities",
        project_id=PROJECT,
        region=REGION,
        batch_id="cluster-identities-{{ ts_nodash | lower }}",
        batch=_pyspark_batch(
            "cluster_identities.py",
            [
                # No --bq-temp-bucket: this job writes via writeMethod=direct (the Storage
                # Write API), not score_pairs.py's GCS-staged indirect method -- see
                # spark_jobs/cluster_identities.py and docs/design-decisions.md, Phase 13.
                "--project",
                PROJECT,
                # --checkpoint-dir/--shuffle-partitions/--max-executors: without these,
                # connected_components' default 200 shuffle partitions vastly oversubscribe
                # this project's real CPUS_ALL_REGIONS-capped 7-worker/28-core ceiling,
                # turning a job that should take minutes into one that runs 76+ minutes and
                # accumulates shuffle-storage cost the whole time it's starved (Phase 14,
                # docs/design-decisions.md).
                "--checkpoint-dir",
                f"gs://{BUCKET}/checkpoints/cluster-identities",
                "--shuffle-partitions",
                "32",
                "--max-executors",
                "7",
                # config/matching.yml owns this now (thresholds.scale.upper): the same FS
                # score is far less precise at 5M records than at 50K (Phase 14 finding),
                # so the scale-tier cutoff is measured separately -- but it lives in config
                # with the other tiers rather than being duplicated here and in the Makefile.
                "--upper-threshold",
                str(SCALE_THRESHOLDS["upper"]),
                "--max-cluster-size",
                "6",
                "--min-cluster-density",
                "0.6",
            ],
        ),
        **DATAPROC_RETRY_KWARGS,
    )

    crosswalk_survivorship = BashOperator(
        task_id="crosswalk_survivorship",
        bash_command=(
            "python /opt/airflow/mdm-scripts/run_matching_bigquery.py "
            f"--project {PROJECT} --tier scale --run-id dedup_dag__{{{{ run_id }}}}"
        ),
        env=SCRIPT_ENV,
        append_env=True,
    )

    dbt_run_serving = BashOperator(
        task_id="dbt_run_serving",
        bash_command=f"cd /opt/airflow/dbt && {DBT_BIN} run --target prod --select serving",
        env=DBT_ENV,
        append_env=True,
    )

    dbt_snapshot = BashOperator(
        task_id="dbt_snapshot",
        bash_command=f"cd /opt/airflow/dbt && {DBT_BIN} snapshot --target prod",
        env=DBT_ENV,
        append_env=True,
    )

    quality_gates = BashOperator(
        task_id="quality_gates",
        bash_command=(
            "python /opt/airflow/mdm-scripts/run_quality_checks_bigquery.py "
            f"--project {PROJECT} --run-id dedup_dag__{{{{ run_id }}}}"
        ),
        env=SCRIPT_ENV,
        append_env=True,
        retries=0,
    )

    (
        score_pairs
        >> cluster_identities
        >> crosswalk_survivorship
        >> dbt_run_serving
        >> dbt_snapshot
        >> quality_gates
    )
