#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATION_FILE="$ROOT_DIR/migrations/0001_phase1_durable_execution.sql"
VERIFICATION_FILE="$ROOT_DIR/tests/verification.sql"
CONTAINER_NAME="${DB_CONTAINER_NAME:-persistent-agent-runtime-postgres}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:16}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-persistent_agent_runtime}"
POSTGRES_PORT="${POSTGRES_PORT:-55432}"
KEEP_DB_CONTAINER="${KEEP_DB_CONTAINER:-0}"

cleanup_container() {
  if [[ "$KEEP_DB_CONTAINER" == "1" ]]; then
    return
  fi

  if [[ "${STARTED_CONTAINER:-0}" == "1" ]]; then
    docker rm -f "$CONTAINER_NAME" >/dev/null
  fi
}

wait_for_postgres() {
  local attempts=0

  until docker exec "$CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [[ "$attempts" -ge 30 ]]; then
      echo "PostgreSQL did not become ready in time" >&2
      exit 1
    fi
    sleep 1
  done
}

apply_sql_file() {
  local sql_file="$1"
  docker exec -i "$CONTAINER_NAME" psql \
    -v ON_ERROR_STOP=1 \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -f - <"$sql_file"
}

reset_database() {
  docker exec -i "$CONTAINER_NAME" psql \
    -v ON_ERROR_STOP=1 \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
}

trap cleanup_container EXIT

if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  if ! docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
    docker start "$CONTAINER_NAME" >/dev/null
  fi
else
  STARTED_CONTAINER=1
  docker run -d \
    --name "$CONTAINER_NAME" \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -p "$POSTGRES_PORT:5432" \
    "$POSTGRES_IMAGE" \
    postgres -c log_statement=all >/dev/null
fi

wait_for_postgres

reset_database
apply_sql_file "$MIGRATION_FILE"
apply_sql_file "$VERIFICATION_FILE"

echo "Schema verification passed"

if [[ "$KEEP_DB_CONTAINER" == "1" ]]; then
  echo "Container retained for inspection: $CONTAINER_NAME"
  echo "View logs with: docker logs $CONTAINER_NAME"
fi
