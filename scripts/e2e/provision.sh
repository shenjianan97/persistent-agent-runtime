#!/usr/bin/env bash
# Provision an isolated test-infra "run" for the current checkout (issue #112).
#
# Used by both `make worker-test` (DB + S3) and `make e2e-test` (+ API/embedding
# ports). Writes $ROOT_DIR/.tmp/e2e.env — the contract file every other target
# sources so they all target THIS run's resources.
#
# Primary checkout (RUN_ID empty)  → today's fixed names/ports (byte-identical).
# Worktree (RUN_ID non-empty)      → own container/bucket + dynamic ports.
#
# Env inputs:  ROOT_DIR, MAIN_ROOT (exported by Make; else derived)
#              E2E_NEED_API=1  → also allocate API + embedding-mock ports
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/e2e/common.sh
. "$SCRIPT_DIR/common.sh"

command -v docker >/dev/null 2>&1 || { echo "❌ Docker is required for tests" >&2; exit 1; }

RUN_ID=$(e2e_run_id)
PG_CONTAINER=$(e2e_pg_container)
BUCKET=$(e2e_s3_bucket)
ENV_FILE=$(e2e_env_file)
MIGRATIONS_DIR=$(e2e_migrations_dir)
NEED_API="${E2E_NEED_API:-}"

# If we abort before finishing (e.g. a migration/seed failure), remove the
# worktree container WE CREATED this run so a failed run never leaks one. Only
# fires for a freshly-created worktree container — never a reused one (it may be
# serving a kept API or a concurrent step) and never the primary (empty RUN_ID),
# which is reused, not disposable.
PROVISION_DONE=
CONTAINER_CREATED=
_provision_cleanup() {
  if [ -n "$RUN_ID" ] && [ "$PROVISION_DONE" != "1" ] && [ -n "$CONTAINER_CREATED" ]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap _provision_cleanup EXIT

mkdir -p "$(dirname "$ENV_FILE")"

# --- Shared LocalStack -----------------------------------------------------
e2e_ensure_localstack

# --- Postgres --------------------------------------------------------------
if [ -z "$RUN_ID" ]; then
  # Primary checkout: fixed container on fixed port 55433 (today's behavior).
  DB_PORT=55433
  if docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    : # already running
  elif docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    docker start "$PG_CONTAINER" >/dev/null
  else
    echo "▶ Creating test Postgres container $PG_CONTAINER (port $DB_PORT)..."
    docker run -d --name "$PG_CONTAINER" \
      --label par-e2e=1 --label par-run= \
      -e POSTGRES_USER="$E2E_DB_USER" \
      -e POSTGRES_PASSWORD="$E2E_DB_PASSWORD" \
      -e POSTGRES_DB="$E2E_DB_NAME" \
      -p "${DB_PORT}:5432" \
      "$E2E_PG_IMAGE" >/dev/null
  fi
else
  # Worktree: reuse-if-healthy, else recreate on an OS-allocated host port.
  if docker ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER" \
      && docker exec "$PG_CONTAINER" pg_isready -U "$E2E_DB_USER" >/dev/null 2>&1; then
    : # healthy — reuse (fast inner loop for E2E_KEEP re-runs)
  else
    # Stale/stopped same-named container from a crashed run would make
    # `docker run --name` fail with "name already in use" — remove it first.
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
    echo "▶ Creating test Postgres container $PG_CONTAINER (RUN_ID=$RUN_ID, dynamic port)..."
    docker run -d --name "$PG_CONTAINER" \
      --label par-e2e=1 --label "par-run=$RUN_ID" \
      -e POSTGRES_USER="$E2E_DB_USER" \
      -e POSTGRES_PASSWORD="$E2E_DB_PASSWORD" \
      -e POSTGRES_DB="$E2E_DB_NAME" \
      -p 0:5432 \
      "$E2E_PG_IMAGE" >/dev/null
    CONTAINER_CREATED=1
  fi
  DB_PORT=$(e2e_discover_pg_port "$PG_CONTAINER")
  if [ -z "$DB_PORT" ]; then
    echo "❌ Could not discover host port for $PG_CONTAINER" >&2
    exit 1
  fi
fi

# Wait for readiness (up to ~60s — generous so concurrent container starts under
# load don't time out spuriously).
for _ in $(seq 1 120); do
  docker exec "$PG_CONTAINER" pg_isready -U "$E2E_DB_USER" >/dev/null 2>&1 && break
  sleep 0.5
done
docker exec "$PG_CONTAINER" pg_isready -U "$E2E_DB_USER" >/dev/null 2>&1 || {
  echo "❌ Test Postgres ($PG_CONTAINER) did not become ready" >&2
  exit 1
}

DB_DSN="postgresql://${E2E_DB_USER}:${E2E_DB_PASSWORD}@${E2E_DB_HOST}:${DB_PORT}/${E2E_DB_NAME}"

# Wait for the HOST port forward to actually accept connections. `docker exec
# pg_isready` only proves the container's INTERNAL socket is up — the published
# host port can lag, especially when two containers are created concurrently.
# Without this, the host-side migrations below race the port forward and silently
# no-op (errors swallowed), leaving an empty schema.
host_ready=
for _ in $(seq 1 60); do
  if PGPASSWORD="$E2E_DB_PASSWORD" psql -h "$E2E_DB_HOST" -p "$DB_PORT" \
      -U "$E2E_DB_USER" -d "$E2E_DB_NAME" -tAc 'SELECT 1' >/dev/null 2>&1; then
    host_ready=1
    break
  fi
  sleep 0.5
done
if [ -z "$host_ready" ]; then
  echo "❌ Postgres host port $DB_PORT for $PG_CONTAINER never accepted connections" >&2
  exit 1
fi

# --- Migrations + seed -----------------------------------------------------
# Apply the full numbered glob every time, errors suppressed — exactly like the
# old test-db-up. Re-applying on an existing schema is a no-op (CREATE/ALTER
# error → swallowed by `|| true`), and applying every run is self-healing: a
# partially-applied schema from a crashed/interrupted prior run is repaired on
# the next provision. (We deliberately do NOT skip on a schema-present check —
# there's no migration-version table, so `tasks` existing can't prove the LATER
# migrations ran.)
for f in "$MIGRATIONS_DIR"/[0-9][0-9][0-9][0-9]_*.sql; do
  PGPASSWORD="$E2E_DB_PASSWORD" psql -h "$E2E_DB_HOST" -p "$DB_PORT" \
    -U "$E2E_DB_USER" -d "$E2E_DB_NAME" -f "$f" -q 2>/dev/null || true
done
# Fail loudly if migrations didn't establish the schema (rather than letting the
# seed below blow up with a cryptic "relation does not exist").
if [ -z "$(PGPASSWORD="$E2E_DB_PASSWORD" psql -h "$E2E_DB_HOST" -p "$DB_PORT" \
    -U "$E2E_DB_USER" -d "$E2E_DB_NAME" -tAc "SELECT to_regclass('public.provider_keys')" 2>/dev/null)" ]; then
  echo "❌ Migrations did not establish schema on $PG_CONTAINER (port $DB_PORT)" >&2
  exit 1
fi
# Seed provider + model rows (idempotent; matches today's test-db-up inline seed,
# which the numbered-migration glob does NOT cover — test_seed.sql is never applied
# by the Make path). Always run: cheap and ON CONFLICT-safe even on reuse.
PGPASSWORD="$E2E_DB_PASSWORD" psql -h "$E2E_DB_HOST" -p "$DB_PORT" \
  -U "$E2E_DB_USER" -d "$E2E_DB_NAME" -q \
  -c "INSERT INTO provider_keys (provider_id, api_key) VALUES ('anthropic', 'e2e-placeholder') ON CONFLICT (provider_id) DO NOTHING;" \
  -c "INSERT INTO models (model_id, provider_id, display_name, is_active, input_microdollars_per_million, output_microdollars_per_million) VALUES ('claude-sonnet-4-6', 'anthropic', 'Claude Sonnet 4.6', true, 3000000, 15000000) ON CONFLICT (provider_id, model_id) DO NOTHING;"

# --- S3 bucket -------------------------------------------------------------
docker exec "$LOCALSTACK_CONTAINER_NAME" awslocal s3 mb "s3://${BUCKET}" >/dev/null 2>&1 || true

# --- API + embedding ports (e2e-test only) ---------------------------------
API_PORT=""
EMB_PORT=""
if [ -n "$NEED_API" ]; then
  if [ -z "$RUN_ID" ]; then
    API_PORT=8081
    EMB_PORT=18099
  else
    # Reuse the prior run's ports if an e2e.env survived (E2E_KEEP=1 or a crash):
    # a still-running kept API stays on E2E_API_PORT, so the Makefile finds it
    # healthy and does NOT spawn a second bootRun — which would orphan the first
    # (its pid file gets overwritten) and break API↔mock port agreement. If the
    # old API is actually dead, its port is free again and the Makefile starts a
    # fresh one there. (No `head`: e2e.env has one line per key, and dropping it
    # avoids the grep|head SIGPIPE under `pipefail`.)
    if [ -f "$ENV_FILE" ]; then
      API_PORT=$(grep -E '^E2E_API_PORT=' "$ENV_FILE" | cut -d= -f2 || true)
      EMB_PORT=$(grep -E '^E2E_EMBEDDING_MOCK_PORT=' "$ENV_FILE" | cut -d= -f2 || true)
    fi
    if [ -z "$API_PORT" ] || [ -z "$EMB_PORT" ]; then
      read -r API_PORT EMB_PORT < <("${PYTHON:-python3}" "$SCRIPT_DIR/free-port.py" 2)
    fi
  fi
fi

# --- Write the contract file -----------------------------------------------
{
  echo "RUN_ID=$RUN_ID"
  echo "E2E_DB_HOST=$E2E_DB_HOST"
  echo "E2E_DB_PORT=$DB_PORT"
  echo "E2E_DB_NAME=$E2E_DB_NAME"
  echo "E2E_DB_USER=$E2E_DB_USER"
  echo "E2E_DB_PASSWORD=$E2E_DB_PASSWORD"
  echo "E2E_DB_DSN=$DB_DSN"
  echo "E2E_PG_CONTAINER=$PG_CONTAINER"
  echo "E2E_PG_IMAGE=$E2E_PG_IMAGE"
  echo "S3_ENDPOINT_URL=$S3_ENDPOINT_URL"
  echo "S3_BUCKET_NAME=$BUCKET"
  echo "LOCALSTACK_CONTAINER_NAME=$LOCALSTACK_CONTAINER_NAME"
  echo "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID"
  echo "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY"
  echo "AWS_REGION=$AWS_REGION"
  echo "E2E_EMBEDDING_MOCK_PROVIDER_ID=$E2E_EMBEDDING_MOCK_PROVIDER_ID"
  if [ -n "$NEED_API" ]; then
    echo "E2E_API_PORT=$API_PORT"
    echo "E2E_API_BASE=http://localhost:${API_PORT}/v1"
    echo "E2E_EMBEDDING_MOCK_PORT=$EMB_PORT"
    echo "E2E_EMBEDDING_MOCK_ENDPOINT=http://127.0.0.1:${EMB_PORT}/v1/embeddings"
  fi
} > "$ENV_FILE"

# Provisioning succeeded — disarm the failure-cleanup trap.
PROVISION_DONE=1

echo "✓ Provisioned run '${RUN_ID:-primary}': DB :$DB_PORT ($PG_CONTAINER), bucket $BUCKET${NEED_API:+, API :$API_PORT, embed :$EMB_PORT}"
echo "  contract: $ENV_FILE"
