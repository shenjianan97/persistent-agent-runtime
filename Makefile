.PHONY: install dev dev-check api-test worker-test e2e-test db-verify clean

install:
	bash scripts/dev-stack.sh --install

dev:
	@DEV_STACK_START_DB_IF_STOPPED=1 $(MAKE) dev-check || { \
		echo "[make dev] Preflight failed. Run 'make install' and 'make dev-check' to diagnose the environment."; \
		exit 1; \
	}
	@bash scripts/dev-stack.sh || { \
		echo "[make dev] Startup failed. Run 'make install' and 'make dev-check' to diagnose the environment."; \
		exit 1; \
	}

dev-check:
	bash scripts/dev-stack.sh --check

api-test:
	cd services/api-service && ./gradlew test

worker-test:
	services/worker-service/.venv/bin/python -m pytest services/worker-service/tests -q

e2e-test:
	services/worker-service/.venv/bin/python -m pytest tests/backend-integration -q

db-verify:
	./infrastructure/database/verify_schema.sh

clean:
	mkdir -p .tmp && rm -f .tmp/*
