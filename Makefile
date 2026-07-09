.PHONY: setup test test-e2e lint format typecheck mutation-test update-deps clean

# Installs from the hash-pinned lock file CI uses, not the loose
# requirements_dev.txt it's compiled from — see .devcontainer/postCreate.sh
# for why that distinction matters. Assumes an already-active venv (the
# devcontainer provides one); run `pre-commit install` once after this to
# get the same checks running locally on every commit.
setup:
	uv pip install --require-hashes -r requirements_dev.lock.txt
	pre-commit install

# Fast suite only (excludes e2e — see [tool.pytest.ini_options] in pyproject.toml).
test:
	pytest

# Runs the built sidecar image against real Mosquitto/BMS-stub containers
# (docker/docker-compose.test.yml) plus the real-timing tests; needs a Docker
# daemon with the compose plugin. First run builds the images.
test-e2e:
	pytest -m e2e

# Mutation testing: not a CI gate (line coverage already gates CI — see
# --cov-fail-under in .github/workflows/tests.yaml), this is a periodic/
# manual signal for finding assertions the test suite is missing. Full run
# takes roughly 30-60 minutes; scope it with
# ONLY_MUTATE=custom_components/pylontech_mqtt/coordinator.py to iterate on
# one file. See scripts/mutmut_report.py's docstring for why survivors need
# human triage rather than a pass/fail threshold.
mutation-test:
	python3 scripts/mutmut_report.py $(if $(ONLY_MUTATE),--only-mutate $(ONLY_MUTATE),)

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .

typecheck:
	mypy
	pyright

update-deps:
	python3 scripts/update_dependencies.py

clean:
	@bash scripts/clean.sh
