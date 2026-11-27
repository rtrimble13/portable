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
.PHONY: help venv install lint types test test-fast cpp schemas docs check clean fixtures coverage version version-check bump release

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

# -n auto runs one pytest-xdist worker per core: the suite spends nearly all
# its time starting `pt` in subprocesses, so it scales with cores. --dist
# loadfile keeps each test file on one worker, so a portfolio template
# (tests/integration/conftest.py) is built once, not once per worker. To
# step through one test under pdb, run pytest yourself without -n.
test:  ## Full suite: unit + property + integration + golden, in parallel
	$(BIN)/pytest -n auto --dist loadfile --cov --cov-report=
	$(BIN)/coverage report
	@echo
	@echo "NOTE: the portable_pt/commands/* modules report 0% here and are NOT"
	@echo "      untested -- tests/integration runs them through the real CLI as"
	@echo "      a subprocess, which coverage does not follow. See issue #40."

test-fast:  ## Unit tests only -- the pre-commit subset
	$(BIN)/pytest -m "unit and not slow" -q -n auto

coverage:  ## Full suite with an HTML coverage report
	$(BIN)/pytest -n auto --dist loadfile --cov --cov-report=
	$(BIN)/coverage html
	@echo "open htmlcov/index.html"

cpp:  ## Configure, build, and run the Catch2 suite
	cmake -S cpp -B build/cpp -DPORTABLE_BUILD_TESTS=ON -DPORTABLE_BUILD_NATIVE=ON
	cmake --build build/cpp --config Release
	ctest --test-dir build/cpp --output-on-failure

schemas:  ## Validate every schemas/*.json and every recorded command output
	$(BIN)/pytest tests/unit/test_json_schemas.py -q

docs:  ## Regenerate docs/schema.md and docs/bmk-schema.md from the DDL comments
	$(BIN)/python -m portable_core.schema.docgen --out docs/schema.md
	$(BIN)/python -m portable_core.schema.docgen --bmk --out docs/bmk-schema.md
	@echo "docs/schema.md and docs/bmk-schema.md regenerated"

fixtures:  ## Rebuild examples/sample.port, sample_prices.csv and sample.bmk from the generators
	$(BIN)/python scripts/build_sample_portfolio.py --out examples/sample.port --force
	$(BIN)/python scripts/build_sample_index.py --out examples/sample.bmk --force

# One version, in one place (ADR 0049). `make version` reports; `make bump` sets
# the version a pull request releases, and merging it publishes the release.
# All three are scripts/release.py and scripts/check_version.py -- see
# docs/releasing.md.
version:  ## This branch's version, main's, and the last release
	$(BIN)/python scripts/release.py

version-check:  ## The version gate CI runs on a pull request, against origin/main
	$(BIN)/python scripts/check_version.py --base origin/main

bump:  ## Set this branch's version: make bump BUMP=patch|minor|major|X.Y.Z [DRY_RUN=1]
	$(if $(BUMP),,$(error Usage: make bump BUMP=patch|minor|major|X.Y.Z [DRY_RUN=1]))
	$(BIN)/python scripts/release.py $(BUMP) $(if $(DRY_RUN),--dry-run,)

release:  ## There is no release step: merging the pull request is the release
	@echo "A release is the merge of a pull request that raises the version (ADR 0049)."
	@echo "On your branch: make bump BUMP=patch|minor|major, commit, and merge."

check: lint types test version-check  ## Everything CI runs (test includes the schema validation)
	@echo "check: green"

clean:  ## Remove build and test artifacts
	rm -rf build dist htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
