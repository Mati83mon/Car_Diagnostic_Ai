# Majster-AI / Car_Diagnostic_AI - developer shortcuts
PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help venv install install-dev fmt fmt-check lint test test-cov check clean run ingest doctor

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install the project with all extras
	$(BIN)/pip install -e ".[all]"

install-dev: venv ## Install with all extras plus the dev toolchain
	$(BIN)/pip install -e ".[all,dev]"

fmt: ## Format with black
	$(BIN)/black majster_ai tests main.py

fmt-check: ## Verify formatting without writing
	$(BIN)/black --check --diff majster_ai tests main.py

lint: ## Lint with flake8
	$(BIN)/flake8 majster_ai tests main.py

test: ## Run the test suite (hardware tests deselected)
	$(BIN)/pytest -m "not hardware"

test-cov: ## Run tests with a coverage report
	$(BIN)/pytest -m "not hardware" --cov=majster_ai --cov-report=term-missing

check: fmt-check lint test ## Everything CI runs

run: ## Start the interactive agent
	$(BIN)/python main.py chat

doctor: ## Print the effective configuration and probe the interface
	$(BIN)/python main.py doctor

ingest: ## (Re)build the workshop-manual index
	$(BIN)/python main.py ingest

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
