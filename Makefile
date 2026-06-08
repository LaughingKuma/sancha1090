# Dev shortcuts. `make` or `make help` lists targets.
.DEFAULT_GOAL := help

SCHED := docker compose exec -T airflow-scheduler bash -c
# ruff isn't installed on the host; run the pinned image. Config-free args match CI.
RUFF  := docker run --rm -v "$$PWD":/io -w /io ghcr.io/astral-sh/ruff:0.15.15
RUFF_ARGS := --select F,B,ARG --line-length 110 --target-version py312

.PHONY: help up down restart ps logs test lint parse check

help: ## List targets
	@grep -hE '^[a-z%-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-9s\033[0m %s\n", $$1, $$2}'

up: ## Start the stack (detached)
	docker compose up -d

down: ## Stop the stack
	docker compose down

restart: ## Recreate the stack
	docker compose up -d --force-recreate

ps: ## Show service status
	docker compose ps

logs: ## Tail logs (scope with S=, e.g. make logs S=risingwave)
	docker compose logs -f $(S)

test: ## Run the pytest suite in the scheduler container
	$(SCHED) "cd /opt/airflow && python -m pytest tests/ -q"

lint: ## Ruff check (real bugs only: F,B,ARG)
	$(RUFF) check $(RUFF_ARGS) .

parse: ## Validate the dbt project (no warehouse needed)
	$(SCHED) "cd /opt/airflow/dbt/sancha1090 && dbt parse --profiles-dir . --target trino"

check: lint parse test ## lint + parse + test
