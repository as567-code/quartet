SHELL := /bin/bash
.DEFAULT_GOAL := status
# Make targets deliberately select THIS repo's own .env (explicit dev convenience); never run
# them from an untrusted checkout, whose .env would then be trusted.
export QUARTET_ENV_FILE := $(CURDIR)/.env

# Pinned scanner (not a project dependency); CI reuses this exact pin via `make`.
SEMGREP := uvx semgrep@1.184.0

.PHONY: setup up down status check sec secrets sast bandit deps-audit test doctor

setup:
	uv sync && uv run pre-commit install

up:
	docker compose up -d --wait

down:
	docker compose down

status:
	docker compose ps

doctor:
	uv run quartet doctor

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src tests scripts
	uv run pytest --cov=quartet --cov-report=term-missing --cov-fail-under=85

sec: secrets sast bandit deps-audit

secrets:
	gitleaks git --redact --no-banner

sast:
	$(SEMGREP) scan --metrics=off --error --config p/python --config p/security-audit --config p/secrets --config .semgrep/quartet.yml --exclude tests/repo_hygiene/fixtures

bandit:
	uv run bandit -q -r src -ll

deps-audit:
	mkdir -p .quartet
	uv export --quiet --frozen --format requirements-txt --no-emit-workspace -o .quartet/requirements.txt
	uv run pip-audit --strict --disable-pip -r .quartet/requirements.txt

test:
	uv run pytest
