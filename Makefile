.PHONY: install test lint format cdk-bootstrap cdk-deploy cdk-destroy clean

PYTHON := python3
PIP := $(PYTHON) -m pip

install:
	$(PIP) install -e ".[dev]"

test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --cov=src --cov-report=html --cov-report=term

lint:
	ruff check src tests
	mypy src

format:
	ruff format src tests
	ruff check --fix src tests

cdk-bootstrap:
	cdk bootstrap

cdk-deploy:
	cdk deploy --all --require-approval never

cdk-destroy:
	cdk destroy --all --force

cdk-synth:
	cdk synth

clean:
	rm -rf .pytest_cache htmlcov .mypy_cache __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
