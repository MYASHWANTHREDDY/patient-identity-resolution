.PHONY: install install-dev lint format test data data-dev data-ci dbt-build evaluate estimate-params match

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

dbt-build:
	DBT_PROFILES_DIR=dbt DUCKDB_PATH=data/$(TIER)/mdm.duckdb dbt build --project-dir dbt --profiles-dir dbt --target dev

evaluate:
	python -m mdm.evaluate --tier $(TIER)

estimate-params:
	python scripts/estimate_fs_params.py --tier $(TIER)

match:
	python scripts/run_matching.py --tier $(TIER)

# `demo` and other pipeline-driving targets are added as the phases that implement them
# land (see PROJECT_CONSTITUTION.md #19). Nothing here claims a capability that doesn't
# exist yet (P3).
