.PHONY: install install-dev lint format test

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

# `data`, `demo`, and other tier-driving targets are added as the phases that implement
# them land (see PROJECT_CONSTITUTION.md #19). Nothing here claims a capability that
# doesn't exist yet (P3).
