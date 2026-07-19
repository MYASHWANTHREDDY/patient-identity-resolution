.PHONY: install install-dev lint format test data data-dev data-ci dbt-build-pre dbt-build evaluate estimate-params match quality-checks pipeline dashboard demo tf-plan tf-apply tf-destroy upload-gcs load-bigquery dbt-build-prod verify-parity package-spark upload-spark-deps dataproc-score-pairs dataproc-cluster-identities match-bigquery quality-checks-bigquery airflow-up airflow-down airflow-logs airflow-trigger-ingestion airflow-trigger-conformance airflow-trigger-dedup

TIER ?= dev
SEED ?= 42

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

lint:
	ruff check .

format:
	ruff format .

test:
	pytest

data:
	python scripts/generate.py --tier $(TIER) --seed $(SEED)
	python scripts/load_local.py --tier $(TIER)

data-dev:
	python scripts/generate.py --tier dev --seed $(SEED)
	python scripts/load_local.py --tier dev

data-ci:
	python scripts/generate.py --tier ci --seed $(SEED)
	python scripts/load_local.py --tier ci

# dbt runs in two passes: serving/* sources are tables scripts/run_matching.py writes,
# so they don't exist until after `match` has run at least once (see
# docs/design-decisions.md, "two-phase dbt flow"). dbt-build-pre builds conformance +
# blocking only; the plain dbt-build (after `match`) builds everything, serving included.
dbt-build-pre:
	DBT_PROFILES_DIR=dbt DUCKDB_PATH=data/$(TIER)/mdm.duckdb dbt build --project-dir dbt --profiles-dir dbt --target dev --exclude path:models/serving snap_member_demographics

dbt-build:
	DBT_PROFILES_DIR=dbt DUCKDB_PATH=data/$(TIER)/mdm.duckdb dbt build --project-dir dbt --profiles-dir dbt --target dev

evaluate:
	python -m mdm.evaluate --tier $(TIER)

estimate-params:
	python scripts/estimate_fs_params.py --tier $(TIER)

match:
	python scripts/run_matching.py --tier $(TIER)

quality-checks:
	python scripts/run_quality_checks.py --tier $(TIER)

# The full local pipeline, in order. Each step depends on the previous one's output.
pipeline: data dbt-build-pre estimate-params match dbt-build evaluate quality-checks

dashboard:
	streamlit run dashboard/app.py -- --tier $(TIER)

# Reviewer path (PROJECT_CONSTITUTION.md #4): dev-tier pipeline end to end, then the
# dashboard. No GCP account needed.
demo: pipeline dashboard

# GCP (Phase 10+). Assumes the project already exists with billing linked and
# terraform/terraform.tfvars set (see docs/design-decisions.md for the manual bootstrap
# steps this repo deliberately doesn't automate: project creation, billing link, budget
# alert).
tf-plan:
	cd terraform && terraform plan

tf-apply:
	cd terraform && terraform apply

tf-destroy:
	cd terraform && terraform destroy

BUCKET ?= $(shell cd terraform && terraform output -raw raw_bucket_name 2>/dev/null)
PROJECT ?= $(shell cd terraform && terraform output -raw project_id 2>/dev/null)

upload-gcs:
	python scripts/upload_to_gcs.py --tier $(TIER) --bucket $(BUCKET)

# Phase 11: dbt against real BigQuery. load-bigquery requires upload-gcs to have run first
# (raw_standard loads from the bucket, not local disk).
load-bigquery:
	python scripts/load_bigquery.py --tier $(TIER) --project $(PROJECT) --bucket $(BUCKET)

dbt-build-prod:
	cd dbt && DBT_PROFILES_DIR=. GCP_PROJECT=$(PROJECT) GCP_REGION=us-central1 dbt build --target prod --exclude path:models/serving snap_member_demographics

verify-parity:
	python scripts/verify_tier_parity.py --tier $(TIER) --project $(PROJECT)

# Phase 12: Spark scoring/clustering on Dataproc Serverless (the project's main cost driver
# -- see docs/design-decisions.md before running these against a real project).
package-spark:
	python scripts/package_spark.py

upload-spark-deps: package-spark
	gsutil cp dist/mdm.zip gs://$(BUCKET)/dependencies/mdm.zip
	gsutil cp dist/rapidfuzz.zip gs://$(BUCKET)/dependencies/rapidfuzz.zip
	gsutil cp spark_jobs/score_pairs.py gs://$(BUCKET)/dependencies/score_pairs.py
	gsutil cp spark_jobs/cluster_identities.py gs://$(BUCKET)/dependencies/cluster_identities.py
	gsutil cp config/fs_params.yml gs://$(BUCKET)/dependencies/fs_params.yml
	gsutil cp config/nicknames.yml gs://$(BUCKET)/dependencies/nicknames.yml

dataproc-score-pairs: upload-spark-deps
	gcloud dataproc batches submit pyspark gs://$(BUCKET)/dependencies/score_pairs.py \
		--project=$(PROJECT) --region=us-central1 \
		--batch=score-pairs-$$(date +%Y%m%d-%H%M%S) \
		--service-account=mdm-pipeline@$(PROJECT).iam.gserviceaccount.com \
		--py-files=gs://$(BUCKET)/dependencies/mdm.zip,gs://$(BUCKET)/dependencies/rapidfuzz.zip \
		--files=gs://$(BUCKET)/dependencies/fs_params.yml,gs://$(BUCKET)/dependencies/nicknames.yml \
		-- --project $(PROJECT) --bq-temp-bucket $(BUCKET)

# --upper-threshold is 20.5, NOT config/matching.yml's 9.0413 (that value is dev/ci-tier
# only -- see the comment there): measured directly against real scale-tier ground truth
# (Phase 14, docs/design-decisions.md), the same FS score is far less precise at 5M records
# than at 50K, so the auto-match cutoff has to move up to hold precision. Re-measure this
# whenever fs_params.yml changes.
dataproc-cluster-identities: upload-spark-deps
	gcloud dataproc batches submit pyspark gs://$(BUCKET)/dependencies/cluster_identities.py \
		--project=$(PROJECT) --region=us-central1 \
		--batch=cluster-identities-$$(date +%Y%m%d-%H%M%S) \
		--service-account=mdm-pipeline@$(PROJECT).iam.gserviceaccount.com \
		--py-files=gs://$(BUCKET)/dependencies/mdm.zip,gs://$(BUCKET)/dependencies/rapidfuzz.zip \
		-- --project $(PROJECT) \
		--checkpoint-dir gs://$(BUCKET)/checkpoints/cluster-identities \
		--shuffle-partitions 32 --max-executors 7 \
		--upper-threshold 20.5 --max-cluster-size 6 --min-cluster-density 0.6

# Phase 13: crosswalk/survivorship and quality checks against BigQuery-resident Dataproc
# output (mdm.pipeline's DuckDB logic, reused unchanged -- see docs/design-decisions.md).
match-bigquery:
	python scripts/run_matching_bigquery.py --project $(PROJECT)

quality-checks-bigquery:
	python scripts/run_quality_checks_bigquery.py --project $(PROJECT)

# Phase 13: Airflow in local Docker (PROJECT_CONSTITUTION.md #15). Requires .env (copied
# from .env.example) with GCP_PROJECT/GCS_BUCKET/GOOGLE_APPLICATION_CREDENTIALS_HOST set.
airflow-up:
	docker compose up --build -d

airflow-down:
	docker compose down

airflow-logs:
	docker compose logs -f airflow-scheduler

# Real, billed GCP work starts here (Dataproc batches inside dedup_dag) -- see
# docs/design-decisions.md before running against a real project.
airflow-trigger-ingestion:
	docker compose exec airflow-scheduler airflow dags trigger ingestion_dag

airflow-trigger-conformance:
	docker compose exec airflow-scheduler airflow dags trigger conformance_dag

airflow-trigger-dedup:
	docker compose exec airflow-scheduler airflow dags trigger dedup_dag
