.PHONY: api-test worker-test e2e-test db-verify clean

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
