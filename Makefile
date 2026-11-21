PYTHON ?= python3

.PHONY: install lint format typecheck test check

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

format:
	$(PYTHON) -m ruff check --fix src tests
	$(PYTHON) -m ruff format src tests

typecheck:
	$(PYTHON) -m mypy --strict src

test:
	$(PYTHON) -m pytest

check: lint typecheck test
