#!/usr/bin/env bash
# Shared helpers for the per-worktree E2E test harness (issue #112).
#
# Sourced by provision.sh / teardown.sh / reap.sh. Keep this POSIX-friendly
# bash (macOS ships bash 3.2): no mapfile, no ${var,,}, no associative arrays.
#
# The "run" is keyed by RUN_ID — a slug of the current checkout directory.
# On the primary checkout (ROOT_DIR == MAIN_ROOT) RUN_ID is empty and every
# name/port falls back to today's fixed single-instance values so CI and the
# main-checkout workflow stay byte-identical. Inside a git worktree RUN_ID is
# non-empty and names are suffixed, ports are dynamically allocated.

# --- Roots -----------------------------------------------------------------
# Make exports ROOT_DIR / MAIN_ROOT via .EXPORT_ALL_VARIABLES, but the scripts
# also work standalone: fall back to pwd / git.
e2e_root_dir() {
  if [ -n "${ROOT_DIR:-}" ]; then
    printf '%s' "$ROOT_DIR"
  else
    pwd
  fi
}

e2e_main_root() {
  if [ -n "${MAIN_ROOT:-}" ]; then
    printf '%s' "$MAIN_ROOT"
    return
  fi
  local d
  d=$(git rev-parse --git-common-dir 2>/dev/null || true)
  if [ -n "$d" ]; then
    (cd "$d/.." && pwd)
  else
    pwd
  fi
}

# RUN_ID: empty on the primary checkout, else a lowercase slug of the worktree
# basename. MUST match the Makefile's RUN_ID derivation.
e2e_run_id() {
  local root main slug
  root=$(e2e_root_dir)
  main=$(e2e_main_root)
  if [ "$root" = "$main" ]; then
    printf ''
    return
  fi
  slug=$(basename "$root" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g' | sed -E 's/^-+//; s/-+$//')
  # A worktree whose basename is all punctuation slugs to empty — which would be
  # misread as the primary checkout and collide with it. Fall back to a stable
  # non-empty token so a worktree is NEVER classified as primary. (cksum is the
  # same binary Make's $(shell) sees, so RUN_ID parity holds on this machine.)
  if [ -z "$slug" ]; then
    slug="wt-$(basename "$root" | cksum | cut -d' ' -f1)"
  fi
  printf '%s' "$slug"
}

# --- Fixed constants (match Makefile defaults) -----------------------------
E2E_DB_NAME="${E2E_DB_NAME:-persistent_agent_runtime_e2e}"
E2E_DB_USER="${E2E_DB_USER:-postgres}"
E2E_DB_PASSWORD="${E2E_DB_PASSWORD:-postgres}"
E2E_DB_HOST="${E2E_DB_HOST:-localhost}"
E2E_PG_IMAGE="${E2E_PG_IMAGE:-pgvector/pgvector:pg16}"
LOCALSTACK_CONTAINER_NAME="${LOCALSTACK_CONTAINER_NAME:-persistent-agent-runtime-localstack}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-http://localhost:4566}"
S3_BUCKET_BASE="${S3_BUCKET_BASE:-platform-artifacts}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-test}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-test}"
AWS_REGION="${AWS_REGION:-us-east-1}"
E2E_EMBEDDING_MOCK_PROVIDER_ID="${E2E_EMBEDDING_MOCK_PROVIDER_ID:-memory-mock}"

# --- Name builders (branch on RUN_ID for byte-compat) ----------------------
# Postgres container name. Primary → par-e2e-postgres; worktree → -<RUN_ID>.
e2e_pg_container() {
  local rid
  rid=$(e2e_run_id)
  if [ -z "$rid" ]; then
    printf 'par-e2e-postgres'
  else
    printf 'par-e2e-postgres-%s' "$rid"
  fi
}

# S3 bucket name. Primary → platform-artifacts; worktree → -<RUN_ID>.
e2e_s3_bucket() {
  local rid
  rid=$(e2e_run_id)
  if [ -z "$rid" ]; then
    printf '%s' "$S3_BUCKET_BASE"
  else
    printf '%s-%s' "$S3_BUCKET_BASE" "$rid"
  fi
}

# Path to the per-run contract file under the worktree's own .tmp/.
e2e_env_file() {
  printf '%s/.tmp/e2e.env' "$(e2e_root_dir)"
}

# Directory holding the SQL migrations.
e2e_migrations_dir() {
  printf '%s/infrastructure/database/migrations' "$(e2e_root_dir)"
}

# docker-compose file (for the shared LocalStack service).
e2e_compose_file() {
  printf '%s/docker-compose.yml' "$(e2e_root_dir)"
}

# Discover the host port mapped to a container's 5432/tcp. Handles IPv4+IPv6
# lines (takes the first) on both Docker Desktop (macOS) and Linux.
e2e_discover_pg_port() {
  local container="$1"
  docker port "$container" 5432/tcp 2>/dev/null | head -1 | sed -E 's/.*:([0-9]+)$/\1/'
}

# Ensure the shared LocalStack container is up and S3 is answering. Idempotent;
# safe to call concurrently (guarded by a running-check, then compose up).
e2e_ensure_localstack() {
  if docker ps --format '{{.Names}}' | grep -qx "$LOCALSTACK_CONTAINER_NAME"; then
    return 0
  fi
  # `|| true`: two concurrent cold provisions may both race `compose up` on the
  # shared fixed container_name and one returns non-zero — tolerate it and let
  # the readiness loop below be the real gate.
  docker compose -f "$(e2e_compose_file)" up -d localstack >/dev/null 2>&1 || true
  local attempts=0
  until docker exec "$LOCALSTACK_CONTAINER_NAME" awslocal s3 ls >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
      echo "❌ LocalStack did not become ready" >&2
      return 1
    fi
    sleep 1
  done
}
