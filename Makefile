.PHONY: install install-dev lint format test data data-dev data-ci dbt-build-pre dbt-build evaluate estimate-params match quality-checks pipeline dashboard demo tf-plan tf-apply tf-destroy upload-gcs

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
upload-gcs:
	python scripts/upload_to_gcs.py --tier $(TIER) --bucket $(BUCKET)
