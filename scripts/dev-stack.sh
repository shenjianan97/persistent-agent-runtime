#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_DIR="$ROOT_DIR/services/worker-service"
API_DIR="$ROOT_DIR/services/api-service"
CONSOLE_DIR="$ROOT_DIR/services/console"
WORKER_VENV_DIR="$WORKER_DIR/.venv"
WORKER_VENV_PYTHON="$WORKER_VENV_DIR/bin/python"
LOCAL_ENV_FILE="$ROOT_DIR/.env.localdev"
DB_CONTAINER_NAME="persistent-agent-runtime-postgres"
START_DB_IF_STOPPED="${DEV_STACK_START_DB_IF_STOPPED:-0}"
FRONTEND_URL="${DEV_STACK_FRONTEND_URL:-http://localhost:5173}"
FRONTEND_BIND_HOST="${DEV_STACK_FRONTEND_BIND_HOST:-0.0.0.0}"

CHECK_ONLY=0
INSTALL_ONLY=0
SERVICE_PIDS=()
SHUTTING_DOWN=0

if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=1
elif [[ "${1:-}" == "--install" ]]; then
    INSTALL_ONLY=1
elif [[ -n "${1:-}" ]]; then
    echo "Usage: $0 [--check|--install]" >&2
    exit 1
fi

log() {
    printf '[dev-stack] %s\n' "$*"
}

log_bold() {
    printf '\033[1m[dev-stack] %s\033[0m\n' "$*"
}

fail() {
    printf '[dev-stack] ERROR: %s\n' "$*" >&2
    if [[ "$INSTALL_ONLY" -eq 1 ]]; then
        printf '[dev-stack] Setup failed. Fix the issue above and re-run: make install\n' >&2
    elif [[ "$CHECK_ONLY" -eq 1 ]]; then
        printf '[dev-stack] Preflight failed. Fix the issue above, then run: make install && make dev-check\n' >&2
    else
        printf '[dev-stack] Startup failed. Try: make install && make dev-check\n' >&2
    fi
    exit 1
}

load_local_env() {
    if [[ ! -f "$LOCAL_ENV_FILE" ]]; then
        return
    fi

    log "Loading local environment from $(basename "$LOCAL_ENV_FILE")"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        [[ "$line" =~ ^[[:space:]]*# ]] && continue

        local key="${line%%=*}"
        local value="${line#*=}"

        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"

        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

        if [[ -z "${!key+x}" ]]; then
            if [[ ${#value} -ge 2 ]]; then
                if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
                    value="${value:1:${#value}-2}"
                elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
                    value="${value:1:${#value}-2}"
                fi
            fi
            export "$key=$value"
        fi
    done <"$LOCAL_ENV_FILE"
}

require_command() {
    local cmd="$1"
    local description="$2"
    command -v "$cmd" >/dev/null 2>&1 || fail "$description is required but was not found on PATH."
}

select_python() {
    local candidate
    for candidate in python3 python; do
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    fail "Python 3.11+ is required to run the worker bootstrap."
}

ensure_runtime_prerequisites() {
    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        log "Checking runtime prerequisites"
    fi
    require_command docker "Docker"
    require_command node "Node.js"
    require_command npm "npm"
    require_command java "Java"
    require_command sed "sed"

    [[ -f "$API_DIR/gradlew" ]] || fail "Missing Gradle wrapper at services/api-service/gradlew."
    [[ -f "$CONSOLE_DIR/package.json" ]] || fail "Missing console package.json."
    [[ -f "$WORKER_DIR/pyproject.toml" ]] || fail "Missing worker pyproject.toml."
    [[ -d "$CONSOLE_DIR/node_modules" ]] || fail "Console dependencies are missing. Run: make install"
    [[ -x "$WORKER_VENV_PYTHON" ]] || fail "Worker virtualenv is missing. Run: make install"
}

ensure_install_prerequisites() {
    require_command node "Node.js"
    require_command npm "npm"
    require_command sed "sed"
    require_command "$(select_python)" "Python 3.11+"

    [[ -f "$CONSOLE_DIR/package.json" ]] || fail "Missing console package.json."
    [[ -f "$WORKER_DIR/pyproject.toml" ]] || fail "Missing worker pyproject.toml."
}

ensure_required_env() {
    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        log "Checking required environment variables"
    fi
    export DB_DSN="${DB_DSN:-postgresql://postgres:postgres@localhost:55432/persistent_agent_runtime}"
    if [[ "$DB_DSN" =~ ^postgresql://([^:]+):([^@]+)@([^:]+):([0-9]+)/(.*)$ ]]; then
        export DB_USER="${BASH_REMATCH[1]}"
        export DB_PASSWORD="${BASH_REMATCH[2]}"
        export DB_HOST="${BASH_REMATCH[3]}"
        export DB_PORT="${BASH_REMATCH[4]}"
        export DB_NAME="${BASH_REMATCH[5]}"
    fi
    export VITE_API_BASE_URL="${VITE_API_BASE_URL:-http://localhost:8080}"
    export VITE_DEV_TASK_CONTROLS_ENABLED="${VITE_DEV_TASK_CONTROLS_ENABLED:-${APP_DEV_TASK_CONTROLS_ENABLED:-false}}"

    [[ -n "${ANTHROPIC_API_KEY:-}" ]] || fail "ANTHROPIC_API_KEY must be set in your shell or .env.localdev."
    if [[ -z "${TAVILY_API_KEY:-}" ]]; then
        log "Warning: TAVILY_API_KEY is not set. The web_search tool will be unavailable."
    fi
}

ensure_db_container() {
    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        log "Checking database container '$DB_CONTAINER_NAME'"
    fi
    local inspect_output
    docker info >/dev/null 2>&1 || fail "Docker is installed but the daemon is not reachable."

    if ! inspect_output="$(docker inspect -f '{{.State.Running}}' "$DB_CONTAINER_NAME" 2>/dev/null)"; then
        fail "Docker container '$DB_CONTAINER_NAME' does not exist. Bootstrap it with: KEEP_DB_CONTAINER=1 ./infrastructure/database/verify_schema.sh"
    fi

    if [[ "$inspect_output" != "true" ]]; then
        if [[ "$CHECK_ONLY" -eq 1 && "$START_DB_IF_STOPPED" != "1" ]]; then
            fail "Docker container '$DB_CONTAINER_NAME' exists but is stopped. Start it manually or run: make dev"
        fi
        log "Starting database container '$DB_CONTAINER_NAME'"
        docker start "$DB_CONTAINER_NAME" >/dev/null
    fi
}

ensure_worker_venv() {
    local python_bin
    python_bin="$(select_python)"

    if [[ ! -d "$WORKER_VENV_DIR" ]]; then
        log "Creating worker virtualenv with $python_bin"
        "$python_bin" -m venv "$WORKER_VENV_DIR"
    else
        log "Reusing existing worker virtualenv"
    fi
}

install_console_dependencies() {
    log "Installing console dependencies"
    (
        cd "$CONSOLE_DIR"
        npm install
    )
}

install_worker_dependencies() {
    ensure_worker_venv
    log "Installing worker dependencies"
    "$WORKER_VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
    (
        cd "$WORKER_DIR"
        "$WORKER_VENV_PYTHON" -m pip install -e '.[dev]'
    )
}

prefix_stream() {
    local name="$1"
    sed -u "s/^/[$name] /"
}

start_service() {
    local name="$1"
    local command="$2"

    bash -lc "$command" \
        > >(prefix_stream "$name") \
        2> >(prefix_stream "$name" >&2) &

    local pid=$!
    SERVICE_PIDS+=("$pid")
    log "Started $name (pid $pid)"
}

cleanup() {
    SHUTTING_DOWN=1
    local pid
    for pid in "${SERVICE_PIDS[@]:-}"; do
        if kill -0 "$pid" >/dev/null 2>&1; then
            kill "$pid" >/dev/null 2>&1 || true
        fi
    done

    for pid in "${SERVICE_PIDS[@]:-}"; do
        wait "$pid" 2>/dev/null || true
    done
}

monitor_services() {
    while true; do
        local pid
        for pid in "${SERVICE_PIDS[@]}"; do
            if ! kill -0 "$pid" >/dev/null 2>&1; then
                local exit_code=0
                wait "$pid" || exit_code=$?
                if [[ "$SHUTTING_DOWN" -eq 1 && "$exit_code" -eq 130 ]]; then
                    exit 0
                fi
                log "A child process exited. Stopping the local stack."
                cleanup
                exit "$exit_code"
            fi
        done
        sleep 1
    done
}

main() {
    load_local_env
    if [[ "$INSTALL_ONLY" -eq 1 ]]; then
        ensure_install_prerequisites
        install_console_dependencies
        install_worker_dependencies
        log "Local development dependencies are installed."
        exit 0
    fi

    ensure_runtime_prerequisites
    ensure_required_env
    ensure_db_container

    if [[ "$CHECK_ONLY" -eq 1 ]]; then
        log "Runtime prerequisites are satisfied."
        log "Local dev stack preflight passed."
        exit 0
    fi

    trap 'cleanup; exit 0' INT TERM
    trap cleanup EXIT

    log "Discovering available models and syncing database..."
    if ! "$WORKER_VENV_PYTHON" "$ROOT_DIR/scripts/discover_models.py"; then
        log "Warning: Model discovery script failed."
    fi

    start_service "console" "cd '$CONSOLE_DIR' && exec npm run dev"
    start_service "api" "cd '$API_DIR' && exec ./gradlew bootRun"
    start_service "worker" "cd '$WORKER_DIR' && source .venv/bin/activate && exec python main.py"

    log "Local dev stack is running. Press Ctrl+C to stop all services."
    log_bold "Frontend endpoint: $FRONTEND_URL (listening on $FRONTEND_BIND_HOST)"
    monitor_services
}

main "$@"
