.PHONY: install install-dev lint format test data data-dev data-ci

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

# `demo` and other pipeline-driving targets are added as the phases that implement them
# land (see PROJECT_CONSTITUTION.md #19). Nothing here claims a capability that doesn't
# exist yet (P3).
