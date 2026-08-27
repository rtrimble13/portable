# `portable` — developer entry points.
#
# Every target here is also what CI runs; `make check` is the whole gate.
# The owner works on Windows: nothing in this file may assume a POSIX-only
# tool beyond what the bootstrap scripts install.

PY ?= python
VENV ?= .venv
ifeq ($(OS),Windows_NT)
  BIN := $(VENV)/Scripts
else
  BIN := $(VENV)/bin
endif

.DEFAULT_GOAL := help
.PHONY: help venv install lint types test test-fast cpp schemas docs check clean fixtures coverage

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## Create the virtualenv (see also scripts/bootstrap.sh / .ps1)
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

install: venv  ## Install portable and its dev dependencies, editable
	$(BIN)/pip install -r requirements-dev.txt -c constraints.txt
	$(BIN)/pip install -e . --no-build-isolation

lint:  ## ruff check + format --check + the two project lint rules
	$(BIN)/ruff check src tests scripts
	$(BIN)/ruff format --check src tests scripts
	$(BIN)/python -m portable_core.lint no-float
	$(BIN)/python -m portable_core.lint gips-language

format:  ## Apply ruff formatting and safe fixes
	$(BIN)/ruff check --fix src tests scripts
	$(BIN)/ruff format src tests scripts

types:  ## mypy (strict on portable_core)
	$(BIN)/mypy

test:  ## Full suite: unit + property + integration + golden
	$(BIN)/coverage run -m pytest
	$(BIN)/coverage report

test-fast:  ## Unit tests only -- the pre-commit subset
	$(BIN)/pytest -m "unit and not slow" -q

coverage:  ## Full suite with an HTML coverage report
	$(BIN)/coverage run -m pytest
	$(BIN)/coverage html
	@echo "open htmlcov/index.html"

cpp:  ## Configure, build, and run the Catch2 suite
	cmake -S cpp -B build/cpp -DPORTABLE_BUILD_TESTS=ON -DPORTABLE_BUILD_NATIVE=ON
	cmake --build build/cpp --config Release
	ctest --test-dir build/cpp --output-on-failure

schemas:  ## Validate every schemas/*.json and every recorded command output
	$(BIN)/pytest tests/unit/test_json_schemas.py -q

docs:  ## Regenerate docs/schema.md from the DDL comments
	$(BIN)/python -m portable_core.schema.docgen > docs/schema.md
	@echo "docs/schema.md regenerated"

fixtures:  ## Rebuild examples/sample.port from its generator script
	$(BIN)/python scripts/build_sample_portfolio.py --out examples/sample.port --force

check: lint types test schemas  ## Everything CI runs
	@echo "check: green"

clean:  ## Remove build and test artifacts
	rm -rf build dist htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
