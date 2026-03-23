# ============================================================
# Persistent Agent Runtime Makefile
# Automates local development, service management, and testing
# ============================================================

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

WORKER_VENV_DIR := $(WORKER_DIR)/.venv
WORKER_VENV_PYTHON := $(WORKER_VENV_DIR)/bin/python

DB_CONTAINER_NAME ?= persistent-agent-runtime-postgres
DB_DSN ?= postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime
VITE_API_BASE_URL ?= http://localhost:8080
APP_DEV_TASK_CONTROLS_ENABLED ?= false

PYTHON ?= $(shell command -v python3.11 || command -v python3 || command -v python)

# Color Output
GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
CYAN := \033[0;36m
NC := \033[0m

.PHONY: help init install install-api install-console install-worker \
        start stop restart start-console start-api start-worker stop-console stop-api stop-worker \
        scale-worker \
        status check check-env check-python db-up db-down db-status db-migrate db-verify \
        test api-test worker-test console-test e2e-test local-ci clean logs


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
	@echo "  $(YELLOW)make start$(NC)          - 🚀 Start all services (use N= for worker count)"
	@echo "  $(YELLOW)make stop$(NC)           - 🛑 Stop all background services"
	@echo "  $(YELLOW)make restart$(NC)        - 🔄 Restart all stack services"
	@echo "  $(YELLOW)make scale-worker N=3$(NC) - ⚖️  Scale workers up or down to N"
	@echo "  $(YELLOW)make status$(NC)         - 📊 Show process and DB statuses"
	@echo "  $(YELLOW)make check$(NC)          - 🔍 Verify environment prerequisites"
	@echo ""
	@echo "$(CYAN)[Docker & Database]$(NC)"
	@echo "  $(YELLOW)make db-up$(NC)          - 🐳 Start PostgreSQL container"
	@echo "  $(YELLOW)make db-down$(NC)        - 🛑 Stop PostgreSQL container"
	@echo "  $(YELLOW)make db-status$(NC)      - 📊 Show DB container status"
	@echo "  $(YELLOW)make db-migrate$(NC)     - 🛠️  Apply SQL migrations safely"
	@echo "  $(YELLOW)make db-verify$(NC)      - 🧪 Verify DB schema (⚠️ DROPS DATA)"
	@echo ""
	@echo "$(CYAN)[Testing]$(NC)"
	@echo "  $(YELLOW)make test$(NC)           - 🧪 Run all tests (API, Worker, Console, E2E)"
	@echo "  $(YELLOW)make api-test$(NC)       -    Run API tests (Gradle)"
	@echo "  $(YELLOW)make worker-test$(NC)    -    Run Worker unit tests (Pytest)"
	@echo "  $(YELLOW)make console-test$(NC)   -    Run Console tests (Vitest)"
	@echo "  $(YELLOW)make e2e-test$(NC)       -    Run Backend E2E tests"
	@echo "  $(YELLOW)make local-ci$(NC)       - ✅ Run local CI script"
	@echo ""
	@echo "$(CYAN)[Dependency Management]$(NC)"
	@echo "  $(YELLOW)make install$(NC)        - 📦 Install all dependencies (API, Console, Worker)"
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
	@command -v node >/dev/null 2>&1 || (echo "$(RED)Node.js is required$(NC)" && exit 1)
	@command -v npm >/dev/null 2>&1 || (echo "$(RED)npm is required$(NC)" && exit 1)
	@command -v java >/dev/null 2>&1 || (echo "$(RED)Java is required$(NC)" && exit 1)
	@command -v sed >/dev/null 2>&1 || (echo "$(RED)sed is required$(NC)" && exit 1)
	@[ -f "$(API_DIR)/gradlew" ] || (echo "$(RED)Gradle wrapper missing in $(API_DIR)$(NC)" && exit 1)
	@[ -x "$(WORKER_VENV_PYTHON)" ] || (echo "$(RED)❌ Worker venv not found. Run 'make install-worker' first.$(NC)" && exit 1)
	@[ -d "$(CONSOLE_DIR)/node_modules" ] || (echo "$(RED)❌ Console node_modules missing. Run 'make install-console' first.$(NC)" && exit 1)
	@echo "$(GREEN)✅ Dependency checks passed$(NC)"

check-python:
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
	@echo "$(YELLOW)🔍 Discovering models...$(NC)"
	@$(WORKER_VENV_PYTHON) services/model-discovery/main.py || (echo "$(RED)❌ Model discovery failed (is the database running?)$(NC)" && exit 1)
	@$(MAKE) start-console
	@$(MAKE) start-api
	@$(MAKE) start-worker N=$(WORKER_COUNT)
	@echo "$(GREEN)✅ All services started in background. Use 'make logs' to watch output.$(NC)"

stop: stop-console stop-api stop-worker
	@echo "$(GREEN)🛑 All services stopped$(NC)"

restart: stop start

start-console:
	@if [ -f $(TMP_DIR)/console.pid ] && ps -p $$(cat $(TMP_DIR)/console.pid) > /dev/null 2>&1; then \
		echo "$(GREEN)✅ Console already running$(NC)"; \
	else \
		echo "$(CYAN)Starting Console...$(NC)"; \
		cd $(CONSOLE_DIR) && nohup npm run dev > $(TMP_DIR)/console.log 2>&1 & echo $$! > $(TMP_DIR)/console.pid; \
		echo "$(GREEN)✅ Console started (Frontend bound to http://localhost:5173)$(NC)"; \
	fi

start-api:
	@if [ -f $(TMP_DIR)/api.pid ] && ps -p $$(cat $(TMP_DIR)/api.pid) > /dev/null 2>&1; then \
		echo "$(GREEN)✅ API Service already running$(NC)"; \
	else \
		echo "$(CYAN)Starting API...$(NC)"; \
		cd $(API_DIR) && nohup ./gradlew bootRun > $(TMP_DIR)/api.log 2>&1 & echo $$! > $(TMP_DIR)/api.pid; \
		echo "$(GREEN)✅ API Service started$(NC)"; \
	fi

start-worker:
	@mkdir -p $(TMP_DIR)
	@n=$(N); n=$${n:-$(WORKER_COUNT)}; \
	started=0; skipped=0; \
	i=1; while [ $$i -le $$n ]; do \
		pidfile=$(TMP_DIR)/worker-$$i.pid; \
		if [ -f $$pidfile ] && ps -p $$(cat $$pidfile) > /dev/null 2>&1; then \
			skipped=$$((skipped + 1)); \
		else \
			rm -f $$pidfile; \
			nohup bash -c "cd $(WORKER_DIR) && source .venv/bin/activate && python main.py" > $(TMP_DIR)/worker-$$i.log 2>&1 & echo $$! > $$pidfile; \
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
	current=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		if ps -p $$(cat "$$pidfile") > /dev/null 2>&1; then \
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
			if [ -f $$pidfile ] && ps -p $$(cat $$pidfile) > /dev/null 2>&1; then \
				slot=$$((slot + 1)); continue; \
			fi; \
			rm -f $$pidfile; \
			nohup bash -c "cd $(WORKER_DIR) && source .venv/bin/activate && python main.py" > $(TMP_DIR)/worker-$$slot.log 2>&1 & echo $$! > $$pidfile; \
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
				kill -9 $$pid 2>/dev/null || true; \
				rm -f "$$pidfile"; \
				stopped=$$((stopped + 1)); \
			fi; \
		done; \
		echo "$(GREEN)✅ Scaled to $$target worker(s)$(NC)"; \
	fi

stop-console:
	@echo "$(YELLOW)Stopping Console...$(NC)"
	@-if [ -f $(TMP_DIR)/console.pid ]; then kill -9 $$(cat $(TMP_DIR)/console.pid) 2>/dev/null || true; rm -f $(TMP_DIR)/console.pid; fi
	@-pkill -f "npm run dev" || true
	@-pkill -f "vite" || true
	@echo "$(GREEN)✅ Console stopped$(NC)"

stop-api:
	@echo "$(YELLOW)Stopping API...$(NC)"
	@-if [ -f $(TMP_DIR)/api.pid ]; then kill -9 $$(cat $(TMP_DIR)/api.pid) 2>/dev/null || true; rm -f $(TMP_DIR)/api.pid; fi
	@-pkill -f "bootRun" || true
	@-lsof -t -i :8080 | xargs kill -9 2>/dev/null || true
	@echo "$(GREEN)✅ API Service stopped$(NC)"

stop-worker:
	@echo "$(CYAN)Stopping all workers...$(NC)"
	@-count=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		pid=$$(cat "$$pidfile"); \
		kill -9 $$pid 2>/dev/null || true; \
		rm -f "$$pidfile"; \
		count=$$((count + 1)); \
	done; \
	if [ $$count -gt 0 ]; then \
		echo "$(GREEN)✅ Stopped $$count worker(s)$(NC)"; \
	fi
	@-pkill -f "$(WORKER_DIR).*main.py" || true
	@echo "$(GREEN)✅ All workers stopped$(NC)"

status:
	@echo "$(YELLOW)📊 System Status:$(NC)"
	@echo "$(CYAN)Database Container:$(NC)"
	@docker ps --filter "name=$(DB_CONTAINER_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | tail -n+2 || echo "  $(RED)Not running$(NC)"
	@echo "$(CYAN)Background Processes:$(NC)"
	@if [ -f $(TMP_DIR)/console.pid ] && ps -p `cat $(TMP_DIR)/console.pid` >/dev/null; then echo "  Console: $(GREEN)Running$(NC) (PID: `cat $(TMP_DIR)/console.pid`)"; else echo "  Console: $(RED)Stopped$(NC)"; fi
	@if [ -f $(TMP_DIR)/api.pid ] && ps -p `cat $(TMP_DIR)/api.pid` >/dev/null; then echo "  API:     $(GREEN)Running$(NC) (PID: `cat $(TMP_DIR)/api.pid`)"; else echo "  API:     $(RED)Stopped$(NC)"; fi
	@worker_count=0; worker_running=0; \
	for pidfile in $(TMP_DIR)/worker-*.pid; do \
		[ -f "$$pidfile" ] || continue; \
		slot=$$(basename "$$pidfile" | sed 's/worker-//;s/.pid//'); \
		pid=$$(cat "$$pidfile"); \
		worker_count=$$((worker_count + 1)); \
		if ps -p $$pid > /dev/null 2>&1; then \
			echo "  Worker $$slot: $(GREEN)Running$(NC) (PID: $$pid)"; \
			worker_running=$$((worker_running + 1)); \
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
	@echo "$(YELLOW)🐳 Ensuring Database Container is running...$(NC)"
	@docker info >/dev/null 2>&1 || (echo "$(RED)❌ Docker daemon is not running. Please start Docker Desktop.$(NC)" && exit 1)
	@if ! docker ps -a --format '{{.Names}}' | grep -Eq "^$(DB_CONTAINER_NAME)$$"; then \
		echo "$(YELLOW)Container $(DB_CONTAINER_NAME) does not exist. Creating it...$(NC)"; \
		docker run -d --name $(DB_CONTAINER_NAME) -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=persistent_agent_runtime -p 55432:5432 postgres:16 postgres -c log_statement=all >/dev/null; \
		echo "$(YELLOW)⏳ Waiting for PostgreSQL to be ready...$(NC)"; \
		sleep 3; \
	fi
	@docker start $(DB_CONTAINER_NAME) >/dev/null 2>&1 || (echo "$(RED)❌ Failed to start container $(DB_CONTAINER_NAME)$(NC)" && exit 1)
	@echo "$(GREEN)✅ DB is up$(NC)"

db-down:
	@echo "$(YELLOW)🛑 Stopping Database Container...$(NC)"
	@docker stop $(DB_CONTAINER_NAME) >/dev/null
	@echo "$(GREEN)✅ DB stopped$(NC)"

db-status:
	@docker ps -a --filter "name=$(DB_CONTAINER_NAME)" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

db-migrate: db-up
	@echo "$(YELLOW)🛠️  Applying migrations to Database...$(NC)"
	@$(foreach file, $(sort $(wildcard $(ROOT_DIR)/infrastructure/database/migrations/*.sql)), \
		echo "Applying $(notdir $(file))..."; \
		docker exec -i $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -U postgres -d persistent_agent_runtime -f - < $(file); \
	)
	@echo "$(GREEN)✅ Migrations applied successfully$(NC)"

db-verify: db-up
	@echo "$(YELLOW)🧪 Running DB Schema Verification (⚠️ DROPS schema)$(NC)"
	@docker exec -i $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -U postgres -d persistent_agent_runtime -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO postgres; GRANT ALL ON SCHEMA public TO public;"
	@$(MAKE) db-migrate
	@echo "$(YELLOW)🧪 Running verification queries...$(NC)"
	@docker exec -i $(DB_CONTAINER_NAME) psql -v ON_ERROR_STOP=1 -U postgres -d persistent_agent_runtime -f - < $(ROOT_DIR)/infrastructure/database/tests/verification.sql
	@echo "$(GREEN)✅ Schema verification passed$(NC)"


# ============================================================
# Testing
# ============================================================

test: api-test worker-test console-test e2e-test
	@echo "$(GREEN)✅ All tests passed!$(NC)"

api-test:
	@echo "$(CYAN)🧪 Running API tests...$(NC)"
	@cd $(API_DIR) && ./gradlew test

worker-test:
	@echo "$(CYAN)🧪 Running Worker tests...$(NC)"
	@$(WORKER_VENV_PYTHON) -m pytest $(WORKER_DIR)/tests -q

console-test:
	@echo "$(CYAN)🧪 Running Console tests...$(NC)"
	@cd $(CONSOLE_DIR) && npm test

e2e-test:
	@echo "$(CYAN)🧪 Running E2E tests...$(NC)"
	@$(WORKER_VENV_PYTHON) -m pytest tests/backend-integration -q

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
