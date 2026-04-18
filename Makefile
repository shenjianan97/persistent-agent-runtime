# ============================================================
# Persistent Agent Runtime Makefile
# Automates local development, service management, and testing
# ============================================================

SHELL := /bin/bash

# --- Configuration & Environment ---

# Load local environment variables if present
-include .env.localdev
.EXPORT_ALL_VARIABLES:

# Default Paths & Variables
ROOT_DIR := $(shell pwd)
WORKER_DIR := $(ROOT_DIR)/services/worker-service
API_DIR := $(ROOT_DIR)/services/api-service
CONSOLE_DIR := $(ROOT_DIR)/services/console
TMP_DIR := $(ROOT_DIR)/.tmp
WORKER_COUNT ?= 1
MIGRATION_FILES := $(sort $(wildcard $(ROOT_DIR)/infrastructure/database/migrations/[0-9][0-9][0-9][0-9]_*.sql))
LANGFUSE_COMPOSE_FILE := $(ROOT_DIR)/tests/fixtures/langfuse/docker-compose.yml
LANGFUSE_DOCKER_PROJECT ?= persistent-agent-runtime-langfuse

WORKER_VENV_DIR := $(WORKER_DIR)/.venv
WORKER_VENV_PYTHON := $(WORKER_VENV_DIR)/bin/python

PYTHON ?= $(shell \
	candidates=$$(ls /opt/homebrew/bin/python3.* /usr/local/bin/python3.* 2>/dev/null | grep -E 'python3\.[0-9]+$$' | sort -t. -k2 -rn); \
	for py in $$candidates python3 python; do \
		if "$$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then \
			echo "$$py"; \
			break; \
		fi; \
	done)

DB_CONTAINER_NAME ?= persistent-agent-runtime-postgres
COMPOSE_FILE := $(ROOT_DIR)/docker-compose.yml
LOCALSTACK_CONTAINER_NAME ?= persistent-agent-runtime-localstack
S3_ENDPOINT_URL ?= http://localhost:4566
S3_BUCKET_NAME ?= platform-artifacts
AWS_ACCESS_KEY_ID ?= test
AWS_SECRET_ACCESS_KEY ?= test
AWS_REGION ?= us-east-1
DB_DSN ?= postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime
DB_HOST ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).hostname or "localhost")' "$(DB_DSN)"),localhost)
DB_PORT ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).port or 55432)' "$(DB_DSN)"),55432)
DB_NAME ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).path.lstrip("/") or "persistent_agent_runtime")' "$(DB_DSN)"),persistent_agent_runtime)
DB_USER ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse, unquote; print(unquote(urlparse(sys.argv[1]).username or "postgres"))' "$(DB_DSN)"),postgres)
DB_PASSWORD ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse, unquote; print(unquote(urlparse(sys.argv[1]).password or "postgres"))' "$(DB_DSN)"),postgres)
SERVER_PORT ?= 8080
VITE_API_BASE_URL ?= http://localhost:8080
APP_DEV_TASK_CONTROLS_ENABLED ?= false
VITE_DEV_TASK_CONTROLS_ENABLED ?= $(APP_DEV_TASK_CONTROLS_ENABLED)
# Langfuse is now configured per-agent via the Console Settings page.
# These defaults are only used by test-langfuse-up / dev-langfuse-up.
LANGFUSE_HOST ?= http://127.0.0.1:3300
LANGFUSE_PUBLIC_KEY ?= pk-lf-local
LANGFUSE_SECRET_KEY ?= sk-lf-local
LANGFUSE_WEB_PORT ?= $(if $(PYTHON),$(shell $(PYTHON) -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).port or 80)' "$(LANGFUSE_HOST)"),3300)

# E2E Test Infrastructure (fully isolated from local dev)
E2E_DB_PORT ?= 55433
E2E_DB_NAME ?= persistent_agent_runtime_e2e
E2E_DB_USER ?= postgres
E2E_DB_PASSWORD ?= postgres
E2E_DB_HOST ?= localhost
E2E_DB_DSN ?= postgresql://$(E2E_DB_USER):$(E2E_DB_PASSWORD)@$(E2E_DB_HOST):$(E2E_DB_PORT)/$(E2E_DB_NAME)
E2E_PG_CONTAINER ?= par-e2e-postgres
E2E_PG_IMAGE ?= pgvector/pgvector:pg16
E2E_API_PORT ?= 8081
E2E_API_BASE ?= http://localhost:$(E2E_API_PORT)/v1
E2E_API_LOG ?= $(TMP_DIR)/e2e-api-service.log

# Color Output
GREEN := $(shell printf '\033[0;32m')
YELLOW := $(shell printf '\033[0;33m')
RED := $(shell printf '\033[0;31m')
CYAN := $(shell printf '\033[0;36m')
NC := $(shell printf '\033[0m')

.PHONY: help init install install-api install-console install-worker \
        start start-with-observability stop restart start-console start-api start-worker stop-console stop-api stop-worker \
        scale-worker \
        status check check-env check-python db-up db-down db-status db-migrate db-reset-verify \
        test-langfuse-up test-langfuse-down test-langfuse-status \
        test test-all api-test worker-test console-test e2e-test e2e-up e2e-down e2e-clean e2e-status \
        test-e2e-langfuse local-ci clean logs


# ============================================================
# Help & One-Click
# ============================================================

help:
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo "$(GREEN)  Persistent Agent Runtime - Makefile Commands$(NC)"
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"
	@echo ""
	@echo "$(CYAN)[One-Click Operations]$(NC)"
	@echo "  $(YELLOW)make init$(NC)           - 🚀 Initial setup (Install deps -> DB up -> Migrate schema)"
	@echo ""
	@echo "$(CYAN)[Service Management]$(NC)"
	@echo "  $(YELLOW)make start$(NC)          - 🚀 Start all services (default: 1 worker)"
	@echo "  $(YELLOW)make start N=3$(NC)      - 🚀 Start all services with 3 workers"
	@echo "  $(YELLOW)make start-worker N=3$(NC) - 👷 Start 3 worker processes only"
	@echo "  $(YELLOW)make scale-worker N=5$(NC) - ⚖️  Scale workers up or down to 5"
	@echo "  $(YELLOW)make stop$(NC)           - 🛑 Stop app services only (DB kept running; use 'make db-down' to stop it)"
	@echo "  $(YELLOW)make restart$(NC)        - 🔄 Restart all stack services"
	@echo "  $(YELLOW)make status$(NC)         - 📊 Show process and DB statuses"
	@echo "  $(YELLOW)make check$(NC)          - 🔍 Verify environment prerequisites"
	@echo ""
	@echo "$(CYAN)[Docker & Database]$(NC)"
	@echo "  $(YELLOW)make db-up$(NC)          - 🐳 Start PostgreSQL container"
	@echo "  $(YELLOW)make db-down$(NC)        - 🛑 Stop PostgreSQL container"
	@echo "  $(YELLOW)make db-status$(NC)      - 📊 Show DB container status"
	@echo "  $(YELLOW)make db-migrate$(NC)     - 🛠️  Apply SQL migrations safely"
	@echo "  $(YELLOW)make db-reset-verify$(NC) - 🧪 Reset and verify DB schema (⚠️ DROPS DATA)"
	@echo "  $(YELLOW)make test-langfuse-up$(NC)     - 🔭 Start a local Langfuse instance for testing"
	@echo "  $(YELLOW)make test-langfuse-down$(NC)  - 🛑 Stop the local Langfuse stack"
	@echo "  $(YELLOW)make test-langfuse-status$(NC) - 📊 Show local Langfuse container status"
	@echo ""
	@echo "$(CYAN)[Testing]$(NC)"
	@echo "  $(YELLOW)make test$(NC)           - 🧪 Run unit tests only (API, Worker, Console)"
	@echo "  $(YELLOW)make test-all$(NC)       - 🧪 Run all tests including E2E"
	@echo "  $(YELLOW)make api-test$(NC)       -    Run API tests (Gradle)"
	@echo "  $(YELLOW)make worker-test$(NC)    -    Run Worker unit tests (Pytest)"
	@echo "  $(YELLOW)make console-test$(NC)   -    Run Console tests (Vitest)"
	@echo "  $(YELLOW)make e2e-test$(NC)       -    Run Backend E2E tests (auto-starts isolated infra)"
	@echo "  $(YELLOW)make test-e2e-langfuse$(NC) - Run Langfuse E2E tests (requires Langfuse + full stack)"
	@echo "  $(YELLOW)make local-ci$(NC)       - ✅ Run local CI script"
	@echo ""
	@echo "$(CYAN)[E2E Infrastructure]$(NC)"
	@echo "  $(YELLOW)make e2e-up$(NC)         - 🧪 Start isolated E2E stack (DB :$(E2E_DB_PORT), API :$(E2E_API_PORT))"
	@echo "  $(YELLOW)make e2e-down$(NC)       - 🛑 Stop E2E stack"
	@echo "  $(YELLOW)make e2e-clean$(NC)      - 🧹 Force-remove E2E containers and leftover processes"
	@echo "  $(YELLOW)make e2e-status$(NC)     - 📊 Show E2E infrastructure status"
	@echo ""
	@echo "$(CYAN)[Dependency Management]$(NC)"
	@echo "  $(YELLOW)make install$(NC)        - 📦 Install all dependencies (API, Console, Worker)"
	@echo ""
	@echo "$(CYAN)[Tips]$(NC)"
	@echo "  $(YELLOW)make -n start N=3$(NC)   - 👀 Preview commands without executing them"
	@echo "  $(YELLOW)APP_DEV_TASK_CONTROLS_ENABLED=true make start$(NC) - 🧪 Enable dev-only task controls locally"
	@echo ""
	@echo "$(CYAN)[Other Tools]$(NC)"
	@echo "  $(YELLOW)make clean$(NC)          - 🧹 Clean temporary/cache files"
	@echo "  $(YELLOW)make logs$(NC)           - 📜 Tail all service logs"
	@echo ""
	@echo "$(GREEN)═══════════════════════════════════════════════════════$(NC)"

init:
	@echo "$(YELLOW)🚀 Bootstrapping local dev stack...$(NC)"
	@$(MAKE) install
	@$(MAKE) db-up
	@$(MAKE) db-migrate
	@echo "$(GREEN)✅ Bootstrap complete. Run 'make start' to launch services.$(NC)"


# ============================================================
# Prerequisites & Checks
# ============================================================

check: check-python check-env
	@echo "$(YELLOW)🔍 Checking dependencies...$(NC)"
	@command -v docker >/dev/null 2>&1 || (echo "$(RED)Docker is required$(NC)" && exit 1)
	@command -v curl >/dev/null 2>&1 || (echo "$(RED)curl is required$(NC)" && exit 1)
	@command -v node >/dev/null 2>&1 || (echo "$(RED)Node.js is required$(NC)" && exit 1)
	@command -v npm >/dev/null 2>&1 || (echo "$(RED)npm is required$(NC)" && exit 1)
	@command -v java >/dev/null 2>&1 || (echo "$(RED)Java is required$(NC)" && exit 1)
	@command -v sed >/dev/null 2>&1 || (echo "$(RED)sed is required$(NC)" && exit 1)
	@command -v pgrep >/dev/null 2>&1 || (echo "$(RED)pgrep is required$(NC)" && exit 1)
	@[ -f "$(API_DIR)/gradlew" ] || (echo "$(RED)Gradle wrapper missing in $(API_DIR)$(NC)" && exit 1)
	@[ -x "$(WORKER_VENV_PYTHON)" ] || (echo "$(RED)❌ Worker venv not found. Run 'make install-worker' first.$(NC)" && exit 1)
	@[ -d "$(CONSOLE_DIR)/node_modules" ] || (echo "$(RED)❌ Console node_modules missing. Run 'make install-console' first.$(NC)" && exit 1)
	@echo "$(GREEN)✅ Dependency checks passed$(NC)"

check-python:
	@[ -n "$(PYTHON)" ] || (echo "$(RED)Python 3.11+ is required$(NC)" && exit 1)
	@$(PYTHON) -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || (echo "$(RED)Python 3.11+ is required$(NC)" && exit 1)

check-env:
	@if [ -z "$$ANTHROPIC_API_KEY" ] && [ -z "$$OPENAI_API_KEY" ]; then \
		echo "$(RED)❌ At least one LLM key (ANTHROPIC_API_KEY or OPENAI_API_KEY) must be set$(NC)"; \
		exit 1; \
	fi


# ============================================================
# Installation
# ============================================================

install: install-api install-console install-worker
	@echo "$(GREEN)✅ All dependencies installed$(NC)"

install-api:
	@echo "$(YELLOW)📦 Compiling API...$(NC)"
	@cd $(API_DIR) && ./gradlew build -x test

install-console:
	@echo "$(YELLOW)📦 Installing Console dependencies...$(NC)"
	@cd $(CONSOLE_DIR) && npm install

install-worker: check-python
	@echo "$(YELLOW)📦 Installing Worker dependencies in .venv...$(NC)"
	@if [ ! -d "$(WORKER_VENV_DIR)" ]; then $(PYTHON) -m venv $(WORKER_VENV_DIR); fi
	@$(WORKER_VENV_PYTHON) -m pip install --upgrade pip setuptools wheel
	@cd $(WORKER_DIR) && $(WORKER_VENV_PYTHON) -m pip install -e '.[dev]'


# ============================================================
# Background Service Management
# ============================================================

start: check
	@mkdir -p $(TMP_DIR)
	@echo "$(YELLOW)🚀 Starting local stack...$(NC)"
	@$(MAKE) db-up
	@echo "$(YELLOW)🔍 Checking DB schema...$(NC)"
	@if ! docker exec -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -At -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" \
		-c "SELECT 1 FROM information_schema.tables WHERE table_name = 'tasks'" 2>/dev/null | grep -qx 1; then \
		echo "$(RED)❌ DB schema not initialized. Run 'make db-migrate' first.$(NC)"; \
		exit 1; \
	fi
	@echo "$(YELLOW)🔍 Discovering models...$(NC)"
	@$(WORKER_VENV_PYTHON) services/model-discovery/main.py || echo "$(YELLOW)⚠️ Model discovery failed; continuing startup with existing models$(NC)"
	@$(MAKE) start-console
	@$(MAKE) start-api
	@$(MAKE) start-worker N=$(if $(N),$(N),$(WORKER_COUNT))
	@if printf '%s' '$(MAKEFLAGS)' | grep -Eq -- '(^|[[:space:]])(n|--just-print|--dry-run|--recon)($|[[:space:]])'; then \
		echo "$(YELLOW)ℹ️ Dry run: skipping startup verification$(NC)"; \
	else \
		echo "$(YELLOW)⏳ Verifying background services...$(NC)"; \
		expected_workers=$(if $(N),$(N),$(WORKER_COUNT)); \
		pid_is_worker() { \
			pid="$$1"; \
			command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
			printf '%s\n' "$$command" | grep -Fq "$(WORKER_DIR)/main.py"; \
		}; \
		wait_for_http() { \
			name="$$1"; pidfile="$$2"; url="$$3"; timeout="$$4"; elapsed=0; \
			while [ $$elapsed -lt $$timeout ]; do \
				if [ -n "$$pidfile" ] && [ ! -f "$$pidfile" ]; then \
					echo "$(RED)❌ $$name failed to create a PID file$(NC)"; \
					return 1; \
				fi; \
				if [ -n "$$pidfile" ]; then \
					pid=$$(cat "$$pidfile"); \
				else \
					pid=""; \
				fi; \
				if [ -n "$$pid" ] && ! ps -p "$$pid" >/dev/null 2>&1; then \
					echo "$(RED)❌ $$name exited before becoming ready$(NC)"; \
					return 1; \
				fi; \
				if curl -fsS "$$url" >/dev/null 2>&1; then \
					echo "$(GREEN)✅ $$name ready$(NC)"; \
					return 0; \
				fi; \
				sleep 1; \
				elapsed=$$((elapsed + 1)); \
			done; \
			echo "$(RED)❌ $$name did not become ready within $$timeout seconds ($$url)$(NC)"; \
			return 1; \
		}; \
		wait_for_workers() { \
			expected="$$1"; timeout="$$2"; elapsed=0; \
			while [ $$elapsed -lt $$timeout ]; do \
				running=0; \
				for pidfile in $(TMP_DIR)/worker-*.pid; do \
					[ -f "$$pidfile" ] || continue; \
					pid=$$(cat "$$pidfile"); \
					if pid_is_worker "$$pid"; then \
						running=$$((running + 1)); \
					fi; \
				done; \
				if [ $$running -eq $$expected ]; then \
					echo "$(GREEN)✅ Workers ready ($$running/$$expected)$(NC)"; \
					return 0; \
				fi; \
				sleep 1; \
				elapsed=$$((elapsed + 1)); \
			done; \
			echo "$(RED)❌ Workers did not stabilize at $$expected process(es) within $$timeout seconds$(NC)"; \
			return 1; \
		}; \
		wait_for_http "Console" "$(TMP_DIR)/console.pid" "http://localhost:5173" 30 && \
		wait_for_http "API Service" "$(TMP_DIR)/api.pid" "http://localhost:$(SERVER_PORT)/actuator/health" 60 && \
		wait_for_workers "$$expected_workers" 20 || { \
			echo "$(RED)❌ Startup verification failed. Use 'make logs' to inspect the service logs.$(NC)"; \
			$(MAKE) stop >/dev/null 2>&1 || true; \
			exit 1; \
		}; \
		echo "$(GREEN)✅ All services started in background and passed startup checks.$(NC)"; \
		echo "$(GREEN)   Console: http://localhost:5173$(NC)"; \
		echo "$(GREEN)   API:     http://localhost:$(SERVER_PORT)$(NC)"; \
		echo "$(YELLOW)   Use 'make logs' to watch output.$(NC)"; \
	fi

start-with-observability:
	@echo "$(YELLOW)ℹ️ Langfuse is now configured per-agent via the Settings page. Use 'make dev-langfuse-up' to start a local instance.$(NC)"
	@$(MAKE) start N=$(if $(N),$(N),$(WORKER_COUNT))

stop: stop-console stop-api stop-worker
	@echo "$(GREEN)✅ All services stopped$(NC)"
	@echo "$(YELLOW)ℹ️  DB container is still running. Run 'make db-down' to stop it.$(NC)"
	@echo "$(YELLOW)ℹ️  If Langfuse is running, stop it with 'make test-langfuse-down'.$(NC)"

restart: stop start

test-langfuse-up:
	@if [ ! -f "$(LANGFUSE_COMPOSE_FILE)" ]; then \
		echo "$(RED)❌ Langfuse compose file not found: $(LANGFUSE_COMPOSE_FILE)$(NC)"; \
		exit 1; \
	fi
	@echo "$(CYAN)Starting test Langfuse stack...$(NC)"
	@docker compose -f "$(LANGFUSE_COMPOSE_FILE)" -p "$(LANGFUSE_DOCKER_PROJECT)" up -d
	@echo "$(GREEN)✅ Langfuse containers launched$(NC)"

test-langfuse-down:
	@if [ -f "$(LANGFUSE_COMPOSE_FILE)" ]; then \
		echo "$(YELLOW)Stopping test Langfuse stack...$(NC)"; \
		docker compose -f "$(LANGFUSE_COMPOSE_FILE)" -p "$(LANGFUSE_DOCKER_PROJECT)" down; \
		echo "$(GREEN)✅ Langfuse stack stopped$(NC)"; \
	else \
		echo "$(YELLOW)ℹ️ No Langfuse compose file found; nothing to stop$(NC)"; \
	fi

test-langfuse-status:
	@if [ ! -f "$(LANGFUSE_COMPOSE_FILE)" ]; then \
		echo "$(RED)❌ Langfuse compose file not found: $(LANGFUSE_COMPOSE_FILE)$(NC)"; \
		exit 1; \
	fi
	@echo "$(CYAN)Langfuse Stack:$(NC)"
	@docker compose -f "$(LANGFUSE_COMPOSE_FILE)" -p "$(LANGFUSE_DOCKER_PROJECT)" ps


start-console:
	@pid_is_console() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/scripts/dev.mjs" || \
			printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/node_modules/.bin/vite --host"; \
	}; \
	find_console_pid() { \
		{ \
			pgrep -f "$(CONSOLE_DIR)/scripts/dev.mjs" || true; \
			pgrep -f "$(CONSOLE_DIR)/node_modules/.bin/vite --host" || true; \
		} | head -n 1; \
	}; \
	if [ -f $(TMP_DIR)/console.pid ] && pid_is_console $$(cat $(TMP_DIR)/console.pid); then \
		echo "$(GREEN)✅ Console already running$(NC)"; \
	elif existing_pid=$$(find_console_pid) && [ -n "$$existing_pid" ]; then \
		echo "$$existing_pid" > $(TMP_DIR)/console.pid; \
		echo "$(GREEN)✅ Console already running$(NC)"; \
	else \
		rm -f $(TMP_DIR)/console.pid; \
		echo "$(CYAN)Starting Console...$(NC)"; \
		nohup bash -lc "cd '$(CONSOLE_DIR)' && export PATH='$(CONSOLE_DIR)/node_modules/.bin':\$$PATH && exec node '$(CONSOLE_DIR)/scripts/dev.mjs'" > $(TMP_DIR)/console.log 2>&1 & echo $$! > $(TMP_DIR)/console.pid; \
		echo "$(GREEN)✅ Console started (Frontend bound to http://localhost:5173)$(NC)"; \
	fi

start-api:
	@pid_is_api() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || \
			printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradlew bootRun"; \
	}; \
	find_api_pid() { \
		{ \
			pgrep -f "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || true; \
			pgrep -f "$(API_DIR)/gradlew bootRun" || true; \
		} | head -n 1; \
	}; \
	if [ -f $(TMP_DIR)/api.pid ] && pid_is_api $$(cat $(TMP_DIR)/api.pid); then \
		echo "$(GREEN)✅ API Service already running$(NC)"; \
	elif existing_pid=$$(find_api_pid) && [ -n "$$existing_pid" ]; then \
		echo "$$existing_pid" > $(TMP_DIR)/api.pid; \
		echo "$(GREEN)✅ API Service already running$(NC)"; \
	else \
		rm -f $(TMP_DIR)/api.pid; \
		echo "$(CYAN)Starting API...$(NC)"; \
		nohup bash -lc "cd '$(API_DIR)' && exec '$(API_DIR)/gradlew' bootRun" > $(TMP_DIR)/api.log 2>&1 & echo $$! > $(TMP_DIR)/api.pid; \
		echo "$(GREEN)✅ API Service started$(NC)"; \
	fi

start-worker:
	@mkdir -p $(TMP_DIR)
	@n=$(N); n=$${n:-$(WORKER_COUNT)}; \
	pid_is_worker() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(WORKER_DIR)/main.py"; \
	}; \
	started=0; skipped=0; \
	i=1; while [ $$i -le $$n ]; do \
		pidfile=$(TMP_DIR)/worker-$$i.pid; \
		if [ -f $$pidfile ] && pid_is_worker $$(cat $$pidfile); then \
			skipped=$$((skipped + 1)); \
		else \
			rm -f $$pidfile; \
			nohup bash -c "cd $(WORKER_DIR) && source .venv/bin/activate && exec python '$(WORKER_DIR)/main.py'" > $(TMP_DIR)/worker-$$i.log 2>&1 & echo $$! > $$pidfile; \
			started=$$((started + 1)); \
		fi; \
		i=$$((i + 1)); \
	done; \
	if [ $$started -gt 0 ]; then \
		echo "$(GREEN)✅ Started $$started worker(s)$(NC)"; \
	fi; \
	if [ $$skipped -gt 0 ]; then \
		echo "$(GREEN)✅ $$skipped worker(s) already running$(NC)"; \
	fi

scale-worker:
	@mkdir -p $(TMP_DIR)
	@target=$(N); target=$${target:-$(WORKER_COUNT)}; \
	pid_is_worker() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(WORKER_DIR)/main.py"; \
	}; \
	stop_worker_pid() { \
		pid="$$1"; \
		if ! pid_is_worker "$$pid"; then \
			return 1; \
		fi; \
		kill -TERM "$$pid" 2>/dev/null || true; \
		attempts=0; \
		while pid_is_worker "$$pid"; do \
			attempts=$$((attempts + 1)); \
			if [ $$attempts -ge 20 ]; then \
				kill -9 "$$pid" 2>/dev/null || true; \
				break; \
			fi; \
			sleep 1; \
		done; \
		return 0; \
	}; \
	current=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		if pid_is_worker $$(cat "$$pidfile"); then \
			current=$$((current + 1)); \
		else \
			rm -f "$$pidfile"; \
		fi; \
	done; \
	if [ $$target -eq $$current ]; then \
		echo "$(GREEN)✅ Already running $$current worker(s), nothing to do$(NC)"; \
	elif [ $$target -gt $$current ]; then \
		need=$$((target - current)); \
		echo "$(CYAN)Scaling up: starting $$need more worker(s) ($$current → $$target)...$(NC)"; \
		started=0; slot=1; \
		while [ $$started -lt $$need ]; do \
			pidfile=$(TMP_DIR)/worker-$$slot.pid; \
			if [ -f $$pidfile ] && pid_is_worker $$(cat $$pidfile); then \
				slot=$$((slot + 1)); continue; \
			fi; \
			rm -f $$pidfile; \
			nohup bash -c "cd $(WORKER_DIR) && source .venv/bin/activate && exec python '$(WORKER_DIR)/main.py'" > $(TMP_DIR)/worker-$$slot.log 2>&1 & echo $$! > $$pidfile; \
			started=$$((started + 1)); slot=$$((slot + 1)); \
		done; \
		echo "$(GREEN)✅ Scaled to $$target worker(s)$(NC)"; \
	else \
		excess=$$((current - target)); \
		echo "$(YELLOW)Scaling down: stopping $$excess worker(s) ($$current → $$target)...$(NC)"; \
		stopped=0; \
			for pidfile in $$(ls -r $(TMP_DIR)/worker-*.pid 2>/dev/null); do \
				[ $$stopped -ge $$excess ] && break; \
				if [ -f "$$pidfile" ]; then \
					pid=$$(cat "$$pidfile"); \
					if stop_worker_pid "$$pid"; then stopped=$$((stopped + 1)); fi; \
					rm -f "$$pidfile"; \
				fi; \
			done; \
			echo "$(GREEN)✅ Scaled to $$target worker(s)$(NC)"; \
		fi

stop-console:
	@echo "$(YELLOW)Stopping Console...$(NC)"
	@-pid_is_console() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/scripts/dev.mjs" || \
			printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/node_modules/.bin/vite --host"; \
	}; \
	stop_console_pid() { \
		pid="$$1"; \
		if ! pid_is_console "$$pid"; then \
			return 1; \
		fi; \
		kill -TERM "$$pid" 2>/dev/null || true; \
		attempts=0; \
		while ps -p "$$pid" >/dev/null 2>&1; do \
			attempts=$$((attempts + 1)); \
			if [ $$attempts -ge 10 ]; then \
				kill -9 "$$pid" 2>/dev/null || true; \
				break; \
			fi; \
			sleep 1; \
		done; \
		return 0; \
	}; \
	list_console_pids() { \
		{ \
			pgrep -f "$(CONSOLE_DIR)/scripts/dev.mjs" || true; \
			pgrep -f "$(CONSOLE_DIR)/node_modules/.bin/vite --host" || true; \
		} | sort -u; \
	}; \
	if [ -f $(TMP_DIR)/console.pid ]; then \
		pid=$$(cat $(TMP_DIR)/console.pid); \
		stop_console_pid "$$pid" || true; \
		rm -f $(TMP_DIR)/console.pid; \
	fi; \
	for pid in $$(list_console_pids); do \
		stop_console_pid "$$pid" || true; \
	done
	@echo "$(GREEN)✅ Console stopped$(NC)"

stop-api:
	@echo "$(YELLOW)Stopping API...$(NC)"
	@-pid_is_api() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || \
			printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradlew bootRun"; \
	}; \
	list_api_pids() { \
		{ \
			pgrep -f "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || true; \
			pgrep -f "$(API_DIR)/gradlew bootRun" || true; \
		} | sort -u; \
	}; \
	if [ -f $(TMP_DIR)/api.pid ]; then \
		pid=$$(cat $(TMP_DIR)/api.pid); \
		if pid_is_api "$$pid"; then kill -9 "$$pid" 2>/dev/null || true; fi; \
		rm -f $(TMP_DIR)/api.pid; \
	fi; \
	for pid in $$(list_api_pids); do \
		kill -9 "$$pid" 2>/dev/null || true; \
	done
	@echo "$(GREEN)✅ API Service stopped$(NC)"

stop-worker:
	@echo "$(CYAN)Stopping all workers...$(NC)"
	@-pid_is_worker() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(WORKER_DIR)/main.py"; \
	}; \
	stop_worker_pid() { \
		pid="$$1"; \
		if ! pid_is_worker "$$pid"; then \
			return 1; \
		fi; \
		kill -TERM "$$pid" 2>/dev/null || true; \
		attempts=0; \
		while pid_is_worker "$$pid"; do \
			attempts=$$((attempts + 1)); \
			if [ $$attempts -ge 20 ]; then \
				kill -9 "$$pid" 2>/dev/null || true; \
				break; \
			fi; \
			sleep 1; \
		done; \
		return 0; \
	}; \
	count=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		pid=$$(cat "$$pidfile"); \
		if stop_worker_pid "$$pid"; then count=$$((count + 1)); fi; \
		rm -f "$$pidfile"; \
	done; \
	if [ $$count -gt 0 ]; then \
		echo "$(GREEN)✅ Stopped $$count worker(s)$(NC)"; \
	fi
	@-pkill -f "$(WORKER_DIR)/main.py" || true
	@echo "$(GREEN)✅ All workers stopped$(NC)"

status:
	@echo "$(YELLOW)📊 System Status:$(NC)"
	@echo "$(CYAN)Database Container:$(NC)"
	@docker ps --filter "name=$(DB_CONTAINER_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | tail -n+2 || echo "  $(RED)Not running$(NC)"
	@echo "$(CYAN)Langfuse Containers:$(NC)"
	@docker compose -f "$(LANGFUSE_COMPOSE_FILE)" -p "$(LANGFUSE_DOCKER_PROJECT)" ps 2>/dev/null || echo "  $(YELLOW)Langfuse stack not running$(NC)"
	@echo "$(CYAN)Background Processes:$(NC)"
	@pid_is_console() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/scripts/dev.mjs" || \
			printf '%s\n' "$$command" | grep -Fq "$(CONSOLE_DIR)/node_modules/.bin/vite --host"; \
	}; \
	find_console_pid() { \
		{ \
			pgrep -f "$(CONSOLE_DIR)/scripts/dev.mjs" || true; \
			pgrep -f "$(CONSOLE_DIR)/node_modules/.bin/vite --host" || true; \
		} | head -n 1; \
	}; \
	pid=""; \
	if [ -f $(TMP_DIR)/console.pid ]; then \
		pid=$$(cat $(TMP_DIR)/console.pid); \
	fi; \
	if [ -z "$$pid" ] || ! pid_is_console "$$pid"; then \
		discovered_pid=$$(find_console_pid); \
		if [ -n "$$discovered_pid" ]; then \
			pid="$$discovered_pid"; \
		fi; \
	fi; \
	if [ -n "$$pid" ]; then \
		if pid_is_console "$$pid"; then \
			echo "  Console: $(GREEN)Running$(NC) (PID: $$pid)"; \
		elif ps -p "$$pid" >/dev/null 2>&1; then \
			echo "  Console: $(RED)Stale$(NC) (PID reused by another process: $$pid)"; \
		else \
			echo "  Console: $(RED)Stopped$(NC) (stale PID: $$pid)"; \
		fi; \
	else \
		echo "  Console: $(RED)Stopped$(NC)"; \
	fi
	@pid_is_api() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || \
			printf '%s\n' "$$command" | grep -Fq "$(API_DIR)/gradlew bootRun"; \
	}; \
	find_api_pid() { \
		{ \
			pgrep -f "$(API_DIR)/gradle/wrapper/gradle-wrapper.jar bootRun" || true; \
			pgrep -f "$(API_DIR)/gradlew bootRun" || true; \
		} | head -n 1; \
	}; \
	pid=""; \
	if [ -f $(TMP_DIR)/api.pid ]; then \
		pid=$$(cat $(TMP_DIR)/api.pid); \
	fi; \
	if [ -z "$$pid" ] || ! pid_is_api "$$pid"; then \
		discovered_pid=$$(find_api_pid); \
		if [ -n "$$discovered_pid" ]; then \
			pid="$$discovered_pid"; \
		fi; \
	fi; \
	if [ -n "$$pid" ]; then \
		if pid_is_api "$$pid"; then \
			echo "  API:     $(GREEN)Running$(NC) (PID: $$pid)"; \
		elif ps -p "$$pid" >/dev/null 2>&1; then \
			echo "  API:     $(RED)Stale$(NC) (PID reused by another process: $$pid)"; \
		else \
			echo "  API:     $(RED)Stopped$(NC) (stale PID: $$pid)"; \
		fi; \
	else \
		echo "  API:     $(RED)Stopped$(NC)"; \
	fi
	@pid_is_worker() { \
		pid="$$1"; \
		command=$$(ps -p "$$pid" -o command= 2>/dev/null || true); \
		printf '%s\n' "$$command" | grep -Fq "$(WORKER_DIR)/main.py"; \
	}; \
	worker_count=0; worker_running=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		slot=$$(basename "$$pidfile" | sed 's/worker-//;s/.pid//'); \
		pid=$$(cat "$$pidfile"); \
		worker_count=$$((worker_count + 1)); \
		if pid_is_worker $$pid; then \
			echo "  Worker $$slot: $(GREEN)Running$(NC) (PID: $$pid)"; \
			worker_running=$$((worker_running + 1)); \
		elif ps -p $$pid > /dev/null 2>&1; then \
			echo "  Worker $$slot: $(RED)Stale$(NC) (PID reused by another process: $$pid)"; \
		else \
			echo "  Worker $$slot: $(RED)Stopped$(NC) (stale PID: $$pid)"; \
		fi; \
	done; \
	if [ $$worker_count -eq 0 ]; then \
		echo "  Workers: $(RED)None$(NC)"; \
	else \
		echo "  $(CYAN)Workers: $$worker_running/$$worker_count running$(NC)"; \
	fi


# ============================================================
# Database Management
# ============================================================

db-up:
	@echo "$(YELLOW)🐳 Ensuring Database and LocalStack containers are running...$(NC)"
	@docker info >/dev/null 2>&1 || (echo "$(RED)❌ Docker daemon is not running. Please start Docker Desktop.$(NC)" && exit 1)
	@if [ "$(DB_HOST)" != "localhost" ] && [ "$(DB_HOST)" != "127.0.0.1" ]; then \
		echo "$(RED)❌ db-up only manages Docker instances on localhost. Current DB_DSN host is '$(DB_HOST)'. Start that database manually instead.$(NC)"; \
		exit 1; \
	fi
	@DB_USER="$(DB_USER)" DB_PASSWORD="$(DB_PASSWORD)" DB_NAME="$(DB_NAME)" DB_PORT="$(DB_PORT)" \
		docker compose -f $(COMPOSE_FILE) up -d postgres localstack
	@echo "$(YELLOW)⏳ Waiting for PostgreSQL to accept connections...$(NC)"
	@attempts=0; \
	until docker exec $(DB_CONTAINER_NAME) env PGPASSWORD="$(DB_PASSWORD)" pg_isready -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" >/dev/null 2>&1; do \
		attempts=$$((attempts + 1)); \
		if [ $$attempts -ge 30 ]; then \
			echo "$(RED)❌ PostgreSQL did not become ready within 30 seconds$(NC)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "$(YELLOW)⏳ Waiting for LocalStack to be ready...$(NC)"
	@attempts=0; \
	until docker exec $(LOCALSTACK_CONTAINER_NAME) awslocal s3 ls >/dev/null 2>&1; do \
		attempts=$$((attempts + 1)); \
		if [ $$attempts -ge 30 ]; then \
			echo "$(RED)❌ LocalStack did not become ready within 30 seconds$(NC)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done
	@echo "$(GREEN)✅ DB and LocalStack are up$(NC)"

db-down:
	@echo "$(YELLOW)🛑 Stopping Database and LocalStack containers...$(NC)"
	@docker compose -f $(COMPOSE_FILE) down
	@echo "$(GREEN)✅ Containers stopped$(NC)"

db-status:
	@docker compose -f $(COMPOSE_FILE) ps

db-migrate: db-up
	@echo "$(YELLOW)🛠️  Applying migrations to Database...$(NC)"
	@docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -c "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())" >/dev/null
	@set -e; \
	for file in $(MIGRATION_FILES); do \
		name=$$(basename "$$file"); \
		if docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -At -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -c "SELECT 1 FROM schema_migrations WHERE filename = '$$name'" | grep -qx 1; then \
			echo "Skipping $$name (already applied)..."; \
			continue; \
		fi; \
		echo "Applying $$name..."; \
		docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -f - < "$$file"; \
		docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -c "INSERT INTO schema_migrations (filename) VALUES ('$$name')"; \
	done
	@echo "$(GREEN)✅ Migrations applied successfully$(NC)"

db-reset-verify: db-up
	@echo "$(YELLOW)🧪 Running DB Schema Verification (⚠️ DROPS schema)$(NC)"
	@docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO public;"
	@$(MAKE) db-migrate
	@echo "$(YELLOW)🧪 Running verification queries...$(NC)"
	@docker exec -i -e PGPASSWORD="$(DB_PASSWORD)" $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U "$(DB_USER)" -d "$(DB_NAME)" -f - < $(ROOT_DIR)/infrastructure/database/tests/verification.sql
	@echo "$(GREEN)✅ Schema verification passed$(NC)"


# ============================================================
# Testing
# ============================================================

test: api-test worker-test console-test
	@echo "$(GREEN)✅ All unit tests passed!$(NC)"

test-all: api-test worker-test console-test e2e-test
	@echo "$(GREEN)✅ All tests passed (unit + E2E)!$(NC)"

api-test:
	@echo "$(CYAN)🧪 Running API tests...$(NC)"
	@cd $(API_DIR) && ./gradlew test

worker-test: test-db-up
	@echo "$(CYAN)🧪 Running Worker tests...$(NC)"
	@E2E_DB_DSN=$(E2E_DB_DSN) $(WORKER_VENV_PYTHON) -m pytest $(WORKER_DIR)/tests -q

console-test:
	@echo "$(CYAN)🧪 Running Console tests...$(NC)"
	@cd $(CONSOLE_DIR) && npm test

e2e-test: e2e-up
	@echo "$(CYAN)🧪 Running E2E tests (isolated infra: DB :$(E2E_DB_PORT), API :$(E2E_API_PORT))...$(NC)"
	@mkdir -p $(TMP_DIR)
	@E2E_DB_HOST=$(E2E_DB_HOST) \
	 E2E_DB_PORT=$(E2E_DB_PORT) \
	 E2E_DB_NAME=$(E2E_DB_NAME) \
	 E2E_DB_USER=$(E2E_DB_USER) \
	 E2E_DB_PASSWORD=$(E2E_DB_PASSWORD) \
	 E2E_DB_DSN=$(E2E_DB_DSN) \
	 E2E_PG_CONTAINER=$(E2E_PG_CONTAINER) \
	 E2E_PG_IMAGE=$(E2E_PG_IMAGE) \
	 E2E_API_PORT=$(E2E_API_PORT) \
	 E2E_API_BASE=$(E2E_API_BASE) \
	 APP_DEV_TASK_CONTROLS_ENABLED=true \
	 $(WORKER_VENV_PYTHON) -m pytest tests/backend-integration -v --tb=short -ra 2>&1 | tee $(TMP_DIR)/e2e-test.log; \
	 e2e_exit=$${PIPESTATUS[0]}; \
	 $(MAKE) e2e-down; \
	 if [ $$e2e_exit -ne 0 ]; then \
	   echo "$(RED)❌ E2E tests failed. Full log: $(TMP_DIR)/e2e-test.log$(NC)"; \
	   echo "$(YELLOW)   API log: $(E2E_API_LOG)$(NC)"; \
	   exit $$e2e_exit; \
	 fi

test-e2e-langfuse: ## Run Langfuse E2E tests (requires: make test-langfuse-up && make start)
	@echo "$(CYAN)🧪 Running Langfuse E2E tests...$(NC)"
	@$(WORKER_VENV_PYTHON) -m pytest tests/e2e-langfuse -ra -q


# ============================================================
# E2E Infrastructure (isolated from local dev)
# ============================================================

test-db-up:
	@command -v docker >/dev/null 2>&1 || { echo "$(RED)❌ Docker is required for tests$(NC)"; exit 1; }
	@# --- Test Postgres ---
	@if docker ps --format '{{.Names}}' | grep -qx '$(E2E_PG_CONTAINER)'; then \
		true; \
	elif docker ps -a --format '{{.Names}}' | grep -qx '$(E2E_PG_CONTAINER)'; then \
		docker start $(E2E_PG_CONTAINER); \
	else \
		echo "$(YELLOW)▶ Creating test Postgres container (port $(E2E_DB_PORT))...$(NC)"; \
		docker run -d --name $(E2E_PG_CONTAINER) \
			-e POSTGRES_USER=$(E2E_DB_USER) \
			-e POSTGRES_PASSWORD=$(E2E_DB_PASSWORD) \
			-e POSTGRES_DB=$(E2E_DB_NAME) \
			-p $(E2E_DB_PORT):5432 \
			$(E2E_PG_IMAGE); \
	fi
	@for i in $$(seq 1 60); do \
		docker exec $(E2E_PG_CONTAINER) pg_isready -U $(E2E_DB_USER) >/dev/null 2>&1 && break; \
		sleep 0.5; \
	done
	@docker exec $(E2E_PG_CONTAINER) pg_isready -U $(E2E_DB_USER) >/dev/null 2>&1 || \
		(echo "$(RED)❌ Test Postgres did not become ready$(NC)" && exit 1)
	@# --- Migrations ---
	@for f in $(MIGRATION_FILES); do \
		PGPASSWORD=$(E2E_DB_PASSWORD) psql -h $(E2E_DB_HOST) -p $(E2E_DB_PORT) -U $(E2E_DB_USER) -d $(E2E_DB_NAME) -f "$$f" -q 2>/dev/null || true; \
	done
	@# --- Seed test models ---
	@PGPASSWORD=$(E2E_DB_PASSWORD) psql -h $(E2E_DB_HOST) -p $(E2E_DB_PORT) -U $(E2E_DB_USER) -d $(E2E_DB_NAME) -q \
		-c "INSERT INTO provider_keys (provider_id, api_key) VALUES ('anthropic', 'e2e-placeholder') ON CONFLICT (provider_id) DO NOTHING;" \
		-c "INSERT INTO models (model_id, provider_id, display_name, is_active, input_microdollars_per_million, output_microdollars_per_million) VALUES ('claude-sonnet-4-6', 'anthropic', 'Claude Sonnet 4.6', true, 3000000, 15000000) ON CONFLICT (provider_id, model_id) DO NOTHING;"
	@# --- LocalStack (shared, for S3 artifact storage) ---
	@if docker ps --format '{{.Names}}' | grep -qx '$(LOCALSTACK_CONTAINER_NAME)'; then \
		true; \
	else \
		DB_USER="$(DB_USER)" DB_PASSWORD="$(DB_PASSWORD)" DB_NAME="$(DB_NAME)" DB_PORT="$(DB_PORT)" \
			docker compose -f $(COMPOSE_FILE) up -d localstack; \
		attempts=0; \
		until docker exec $(LOCALSTACK_CONTAINER_NAME) awslocal s3 ls >/dev/null 2>&1; do \
			attempts=$$((attempts + 1)); \
			if [ $$attempts -ge 30 ]; then \
				echo "$(RED)❌ LocalStack did not become ready$(NC)"; \
				exit 1; \
			fi; \
			sleep 1; \
		done; \
	fi

e2e-up: test-db-up
	@mkdir -p $(TMP_DIR)
	@echo "$(YELLOW)🧪 Starting E2E infrastructure (DB :$(E2E_DB_PORT), API :$(E2E_API_PORT))...$(NC)"
	@echo "  $(GREEN)✓ E2E Postgres ready$(NC)"
	@echo "  $(GREEN)✓ E2E migrations applied$(NC)"
	@echo "  $(GREEN)✓ E2E test models seeded$(NC)"
	@# --- API ---
	@if curl -sf http://localhost:$(E2E_API_PORT)/v1/health >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ E2E API already running$(NC)"; \
	else \
		echo "  $(YELLOW)▶ Starting E2E API on port $(E2E_API_PORT)...$(NC)"; \
		DB_HOST=$(E2E_DB_HOST) DB_PORT=$(E2E_DB_PORT) DB_NAME=$(E2E_DB_NAME) \
		DB_USER=$(E2E_DB_USER) DB_PASSWORD=$(E2E_DB_PASSWORD) \
		SERVER_PORT=$(E2E_API_PORT) APP_DEV_TASK_CONTROLS_ENABLED=true \
		nohup $(API_DIR)/gradlew bootRun -p $(API_DIR) > $(E2E_API_LOG) 2>&1 & \
		echo $$! > $(TMP_DIR)/e2e-api.pid; \
		echo "  $(YELLOW)⏳ Waiting for E2E API health...$(NC)"; \
		for i in $$(seq 1 120); do \
			curl -sf http://localhost:$(E2E_API_PORT)/v1/health >/dev/null 2>&1 && break; \
			sleep 1; \
		done; \
		if curl -sf http://localhost:$(E2E_API_PORT)/v1/health >/dev/null 2>&1; then \
			echo "  $(GREEN)✓ E2E API ready$(NC)"; \
		else \
			echo "$(RED)❌ E2E API failed to start. Check $(E2E_API_LOG)$(NC)"; \
			exit 1; \
		fi; \
	fi
	@echo "$(GREEN)✅ E2E infrastructure ready$(NC)"

e2e-down:
	@echo "$(YELLOW)🛑 Stopping E2E infrastructure...$(NC)"
	@# --- API ---
	@if [ -f $(TMP_DIR)/e2e-api.pid ]; then \
		pid=$$(cat $(TMP_DIR)/e2e-api.pid); \
		if kill -0 $$pid 2>/dev/null; then \
			echo "  $(YELLOW)▶ Stopping E2E API (PID $$pid)...$(NC)"; \
			kill $$pid 2>/dev/null || true; \
			for i in $$(seq 1 20); do kill -0 $$pid 2>/dev/null || break; sleep 1; done; \
			kill -9 $$pid 2>/dev/null || true; \
		fi; \
		rm -f $(TMP_DIR)/e2e-api.pid; \
	fi
	@# Also kill any bootRun on the E2E port
	@lsof -ti :$(E2E_API_PORT) | xargs kill 2>/dev/null || true
	@echo "  $(GREEN)✓ E2E API stopped$(NC)"
	@# --- Postgres ---
	@if docker ps --format '{{.Names}}' | grep -qx '$(E2E_PG_CONTAINER)'; then \
		echo "  $(YELLOW)▶ Stopping E2E Postgres...$(NC)"; \
		docker stop $(E2E_PG_CONTAINER); \
	fi
	@echo "  $(GREEN)✓ E2E Postgres stopped$(NC)"
	@echo "$(GREEN)✅ E2E infrastructure stopped$(NC)"

e2e-clean:
	@echo "$(YELLOW)🧹 Force-cleaning E2E leftovers...$(NC)"
	@# Kill any process on E2E API port
	@lsof -ti :$(E2E_API_PORT) | xargs kill -9 2>/dev/null || true
	@rm -f $(TMP_DIR)/e2e-api.pid $(TMP_DIR)/e2e-api-service.log $(TMP_DIR)/e2e-test.log
	@# Remove E2E Postgres container entirely
	@docker rm -f $(E2E_PG_CONTAINER) 2>/dev/null || true
	@echo "$(GREEN)✅ E2E leftovers cleaned$(NC)"

e2e-status:
	@echo "$(CYAN)📊 E2E Infrastructure Status$(NC)"
	@echo "   DB Port:  $(E2E_DB_PORT)  (local dev: $(DB_PORT))"
	@echo "   API Port: $(E2E_API_PORT)  (local dev: $(SERVER_PORT))"
	@echo "   DB Name:  $(E2E_DB_NAME)"
	@printf "   Postgres: "; \
	if docker ps --format '{{.Names}}' | grep -qx '$(E2E_PG_CONTAINER)'; then \
		echo "$(GREEN)running$(NC)"; \
	elif docker ps -a --format '{{.Names}}' | grep -qx '$(E2E_PG_CONTAINER)'; then \
		echo "$(YELLOW)stopped$(NC)"; \
	else \
		echo "$(RED)not created$(NC)"; \
	fi
	@printf "   API:      "; \
	if curl -sf http://localhost:$(E2E_API_PORT)/v1/health >/dev/null 2>&1; then \
		echo "$(GREEN)healthy$(NC)"; \
	elif [ -f $(TMP_DIR)/e2e-api.pid ] && kill -0 $$(cat $(TMP_DIR)/e2e-api.pid 2>/dev/null) 2>/dev/null; then \
		echo "$(YELLOW)starting$(NC)"; \
	else \
		echo "$(RED)not running$(NC)"; \
	fi

local-ci:
	@echo "$(CYAN)🚀 Running Local CI checks...$(NC)"
	@bash scripts/local-ci.sh


# ============================================================
# Utilities
# ============================================================

clean:
	@echo "$(YELLOW)🧹 Cleaning temporary files...$(NC)"
	@rm -rf $(TMP_DIR)/*
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@cd $(API_DIR) && ./gradlew clean
	@echo "$(GREEN)✅ Clean complete$(NC)"

logs:
	@echo "$(YELLOW)📜 Tailing logs (Ctrl+C to exit)...$(NC)"
	@tail -f $(TMP_DIR)/*.log 2>/dev/null || echo "$(RED)No logs found. Are services started?$(NC)"
