.PHONY: help setup lint format format-check typecheck test test-unit test-integration \
       check migrate-up migrate-down docker-up docker-down clean

# Load .env if present (exports vars for migrate, docker compose, etc.)
ifneq (,$(wildcard .env))
  ENV_VARS := $(shell sed -e '/^\#/d' -e '/^$$/d' -e 's/\#.*//' -e 's/ *$$//' .env)
  $(foreach var,$(ENV_VARS),$(eval export $(var)))
endif

# golang-migrate — auto-downloaded to .bin/ if not present
MIGRATE_VERSION := 4.18.3
MIGRATE_BIN := .bin/migrate
MIGRATE_ARCH := $(shell uname -m | sed 's/x86_64/amd64/' | sed 's/aarch64/arm64/')
MIGRATE_OS := $(shell uname -s | tr A-Z a-z)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

$(MIGRATE_BIN):
	@mkdir -p .bin
	@echo "Downloading golang-migrate v$(MIGRATE_VERSION)..."
	@curl -sL https://github.com/golang-migrate/migrate/releases/download/v$(MIGRATE_VERSION)/migrate.$(MIGRATE_OS)-$(MIGRATE_ARCH).tar.gz \
		| tar xz -C .bin
	@chmod +x $(MIGRATE_BIN)
	@echo "golang-migrate installed at $(MIGRATE_BIN)"

setup:  ## Install dependencies, Playwright browser, pre-commit hooks, and golang-migrate
	@test -f .env || (cp example.env .env && echo "Created .env from example.env — edit with your credentials")
	@test -f receipt-index.yaml || (cp example.receipt-index.yaml receipt-index.yaml && echo "Created receipt-index.yaml from example — edit with your settings")
	uv sync --all-extras
	PLAYWRIGHT_BROWSERS_PATH=.playwright uv run playwright install chromium
	uv run pre-commit install
	$(MAKE) $(MIGRATE_BIN)

lint:  ## Run linting (ruff check)
	uv run ruff check src/ tests/

format:  ## Format code (ruff format)
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

format-check:  ## Check formatting without modifying files
	uv run ruff format --check src/ tests/
	uv run ruff check src/ tests/

typecheck:  ## Run type checking (mypy)
	uv run mypy src/

test:  ## Run all tests with coverage
	uv run pytest

test-unit:  ## Run unit tests only
	uv run pytest tests/unit/ --cov-fail-under=80

test-integration:  ## Run integration tests only (requires docker-up)
	uv run pytest tests/integration/ -m integration --cov-fail-under=60

check:  ## Run all checks (lint, format, typecheck, unit tests)
	$(MAKE) format-check
	$(MAKE) typecheck
	$(MAKE) test-unit

migrate-up: $(MIGRATE_BIN)  ## Run all migrations in schema dependency order (uses MIGRATION_DATABASE_URL)
	$(MIGRATE_BIN) -path db/migrations/public -database "$${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_public" up
	$(MIGRATE_BIN) -path db/migrations/receipt -database "$${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_receipt" up

migrate-down: $(MIGRATE_BIN)  ## Roll back the last migration for each schema (reverse order)
	$(MIGRATE_BIN) -path db/migrations/receipt -database "$${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_receipt" down 1
	$(MIGRATE_BIN) -path db/migrations/public -database "$${MIGRATION_DATABASE_URL}&x-migrations-table=schema_migrations_public" down 1

docker-up:  ## Start Docker services (PostgreSQL, GreenMail)
	docker compose up -d

docker-down:  ## Stop Docker services
	docker compose down

clean:  ## Remove build artifacts
	rm -rf dist/ build/ *.egg-info/ htmlcov/ .coverage .mypy_cache/ .ruff_cache/ .pytest_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
