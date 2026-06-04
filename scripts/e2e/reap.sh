#!/usr/bin/env bash
# GC test-infra leftovers from crashed agents (issue #112).
#
# Sledgehammer by design: removes EVERY container labeled par-e2e=1 and EVERY
# per-run bucket (platform-artifacts-<slug>). A leaked container from a crashed
# agent is usually still running, so we cannot distinguish "live" from "stale"
# reliably — run this as fleet hygiene when agents are idle. A single active
# agent never needs it: deterministic per-worktree names mean a re-run replaces
# its own container. Never touches the shared `platform-artifacts` bucket.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=scripts/e2e/common.sh
. "$SCRIPT_DIR/common.sh"

command -v docker >/dev/null 2>&1 || { echo "❌ Docker is required" >&2; exit 1; }

# --- Containers ------------------------------------------------------------
ids=$(docker ps -aq --filter label=par-e2e=1 || true)
if [ -n "$ids" ]; then
  echo "▶ Removing $(printf '%s\n' "$ids" | grep -c . | tr -d ' ') labeled test container(s)..."
  # shellcheck disable=SC2086
  docker rm -f $ids >/dev/null 2>&1 || true
else
  echo "  no labeled test containers to reap"
fi

# --- Per-run buckets (never the shared base bucket) ------------------------
if docker ps --format '{{.Names}}' | grep -qx "$LOCALSTACK_CONTAINER_NAME"; then
  buckets=$(docker exec "$LOCALSTACK_CONTAINER_NAME" awslocal s3 ls 2>/dev/null \
    | awk '{print $3}' | grep -E "^${S3_BUCKET_BASE}-." || true)
  for b in $buckets; do
    echo "▶ Dropping per-run bucket $b"
    docker exec "$LOCALSTACK_CONTAINER_NAME" awslocal s3 rb "s3://${b}" --force >/dev/null 2>&1 || true
  done
fi

echo "✓ Reap complete"
