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

data-dev:
	python scripts/generate.py --tier dev --seed $(SEED)

data-ci:
	python scripts/generate.py --tier ci --seed $(SEED)

# `demo` and other pipeline-driving targets are added as the phases that implement them
# land (see PROJECT_CONSTITUTION.md #19). Nothing here claims a capability that doesn't
# exist yet (P3).
