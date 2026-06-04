#!/usr/bin/env bash
# Tear down the current run's test infra (issue #112). Idempotent and safe to
# call even when nothing was provisioned. Sources $ROOT_DIR/.tmp/e2e.env so it
# always targets THIS run's container/bucket/API.
#
# E2E_KEEP=1  → leave everything up (fast fix→test loop; next run reuses it).
#
# Primary checkout: `docker stop` the shared container (reuse, byte-identical
# with today's `e2e-down`). Worktree: `docker rm -f` the container and drop the
# run's bucket (disposable). Never drops the shared `platform-artifacts` bucket.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/e2e/common.sh
. "$SCRIPT_DIR/common.sh"

ROOT=$(e2e_root_dir)
ENV_FILE=$(e2e_env_file)
TMP_DIR="$ROOT/.tmp"

if [ "${E2E_KEEP:-}" = "1" ]; then
  echo "↺ E2E_KEEP=1 — leaving run infra up for reuse"
  exit 0
fi

if [ ! -f "$ENV_FILE" ]; then
  exit 0
fi

# Load the run's identifiers.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# --- API (if this run started one) -----------------------------------------
if [ -f "$TMP_DIR/e2e-api.pid" ]; then
  pid=$(cat "$TMP_DIR/e2e-api.pid" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$TMP_DIR/e2e-api.pid"
fi
# Best-effort port sweep (lsof may be absent on minimal Linux).
if [ -n "${E2E_API_PORT:-}" ] && command -v lsof >/dev/null 2>&1; then
  p=$(lsof -ti ":$E2E_API_PORT" 2>/dev/null || true)
  [ -n "$p" ] && kill $p 2>/dev/null || true
fi

# --- Postgres --------------------------------------------------------------
# Remove the container on BOTH primary and worktree — a test run cleans up fully
# by default, regardless of checkout. E2E_KEEP=1 (handled above) is the opt-out
# for a fast fix→test loop. CI runs pytest directly (not this Make path), so it's
# unaffected; the legacy `make e2e-up`/`e2e-down` targets still stop-and-reuse the
# primary container for manual lifecycles.
docker rm -f "$E2E_PG_CONTAINER" >/dev/null 2>&1 || true

# --- S3 bucket (worktree only; never the shared base bucket) ---------------
if [ -n "${RUN_ID:-}" ] && [ -n "${S3_BUCKET_NAME:-}" ] && [ "$S3_BUCKET_NAME" != "$S3_BUCKET_BASE" ]; then
  docker exec "$LOCALSTACK_CONTAINER_NAME" awslocal s3 rb "s3://${S3_BUCKET_NAME}" --force >/dev/null 2>&1 || true
fi

rm -f "$ENV_FILE"
echo "✓ Tore down run '${RUN_ID:-primary}'"
