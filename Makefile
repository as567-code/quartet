SHELL := /bin/bash
.DEFAULT_GOAL := status
export QUARTET_ENV_FILE := $(CURDIR)/.env

.PHONY: setup up down status check sec test doctor

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

sec:
	@echo "sec: wired in Task 4"

test:
	uv run pytest
