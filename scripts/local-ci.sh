#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_PY="${WORKER_PY:-$ROOT_DIR/services/worker-service/.venv/bin/python}"
PG_CONTAINER="${PG_CONTAINER:-persistent-agent-runtime-postgres}"
DB_NAME="${DB_NAME:-persistent_agent_runtime}"
API_DIR="$ROOT_DIR/services/api-service"
WORKER_DIR="$ROOT_DIR/services/worker-service"
CONSOLE_DIR="$ROOT_DIR/services/console"
TMP_DIR="$ROOT_DIR/.tmp"
WORKER_INTEGRATION_LOG="$TMP_DIR/worker-integration.log"
E2E_LOG="$TMP_DIR/e2e.log"
E2E_API_LOG="$TMP_DIR/e2e-api-service.log"

log() {
    printf '[local-ci] %s\n' "$*"
}

fail() {
    printf '[local-ci] ERROR: %s\n' "$*" >&2
    exit 1
}

find_python311() {
    if [[ -n "${PY311:-}" ]]; then
        printf '%s\n' "$PY311"
        return 0
    fi

    local candidate
    for candidate in python3.11 python3 python; do
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done

    fail "Python 3.11+ is required. Set PY311=/path/to/python3.11 if it is not on PATH."
}

require_executable() {
    local path="$1"
    local description="$2"
    [[ -x "$path" ]] || fail "$description is missing or not executable: $path"
}

require_directory() {
    local path="$1"
    local description="$2"
    [[ -d "$path" ]] || fail "$description is missing: $path"
}

docker_psql() {
    docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d "$DB_NAME" "$@"
}

preflight() {
    mkdir -p "$TMP_DIR"

    local python311
    python311="$(find_python311)"

    local running
    running="$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null || true)"
    [[ "$running" == "true" ]] || fail "PostgreSQL container '$PG_CONTAINER' is not running."

    local port_output
    port_output="$(docker port "$PG_CONTAINER" 5432/tcp 2>/dev/null || true)"
    grep -q '55432' <<<"$port_output" || fail "PostgreSQL container '$PG_CONTAINER' is not exposed on host port 55432."

    require_executable "$WORKER_PY" "Worker virtualenv python"
    require_directory "$CONSOLE_DIR/node_modules" "Console node_modules"
    require_executable "$API_DIR/gradlew" "Gradle wrapper"

    "$python311" --version >/dev/null
    "$API_DIR/gradlew" -v >/dev/null
}

reset_db_schema() {
    log "Resetting database schema in '$DB_NAME'"
    docker_psql <<'SQL'
DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO postgres;
GRANT ALL ON SCHEMA public TO public;
SQL
}

apply_migrations() {
    log "Applying database migrations"

    local sql_file
    for sql_file in \
        "$ROOT_DIR/infrastructure/database/migrations/0001_phase1_durable_execution.sql" \
        "$ROOT_DIR/infrastructure/database/migrations/0002_worker_registry.sql" \
        "$ROOT_DIR/infrastructure/database/migrations/0003_dynamic_models.sql" \
        "$ROOT_DIR/infrastructure/database/migrations/test_seed.sql"; do
        docker_psql <"$sql_file"
    done
}

assert_no_pytest_skips() {
    local log_file="$1"
    local stage_name="$2"

    if grep -Eoq '(^|[^0-9])[1-9][0-9]* skipped' "$log_file"; then
        fail "$stage_name reported skipped tests. See $log_file"
    fi
}

run_api_unit_tests() {
    log "Running API unit tests"
    (
        cd "$API_DIR"
        ./gradlew test
    )
}

run_worker_unit_tests() {
    log "Running worker unit tests"
    (
        cd "$WORKER_DIR"
        "$WORKER_PY" -m pytest tests \
            --ignore=tests/test_checkpointer_integration.py \
            --ignore=tests/test_integration.py \
            -q
    )
}

run_api_integration_tests() {
    log "Running API integration tests"
    reset_db_schema
    apply_migrations
    (
        cd "$API_DIR"
        INTEGRATION_TESTS_ENABLED=true ./gradlew test
    )
}

run_worker_integration_tests() {
    log "Running worker integration tests"
    reset_db_schema
    apply_migrations
    rm -f "$WORKER_INTEGRATION_LOG"
    (
        cd "$WORKER_DIR"
        E2E_DB_DSN="postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime" \
            "$WORKER_PY" -m pytest \
            tests/test_checkpointer_integration.py \
            tests/test_integration.py \
            -ra -q | tee "$WORKER_INTEGRATION_LOG"
    )
    assert_no_pytest_skips "$WORKER_INTEGRATION_LOG" "Worker integration tests"
}

run_backend_e2e_tests() {
    log "Running backend E2E tests"
    reset_db_schema
    apply_migrations
    rm -f "$E2E_LOG" "$E2E_API_LOG"

    if ! (
        cd "$ROOT_DIR"
        E2E_DB_HOST=localhost \
        E2E_DB_PORT=55432 \
        E2E_DB_NAME=persistent_agent_runtime \
        E2E_DB_USER=postgres \
        E2E_DB_PASSWORD=postgres \
        APP_DEV_TASK_CONTROLS_ENABLED=true \
            "$WORKER_PY" -m pytest tests/backend-integration -ra -q | tee "$E2E_LOG"
    ); then
        if [[ -f "$E2E_API_LOG" ]]; then
            log "Backend E2E failed during or after API startup. Inspect $E2E_API_LOG"
        fi
        return 1
    fi

    assert_no_pytest_skips "$E2E_LOG" "Backend E2E tests"
}

run_console_tests() {
    log "Running console Vitest suite"
    (
        cd "$CONSOLE_DIR"
        npm test
    )
}

main() {
    preflight
    run_api_unit_tests
    run_worker_unit_tests
    run_api_integration_tests
    run_worker_integration_tests
    run_backend_e2e_tests
    run_console_tests
    log "Local CI workflow completed successfully."
}

main "$@"
