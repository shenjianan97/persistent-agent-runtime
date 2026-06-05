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
# Only per-WORKTREE containers (non-empty par-run label) — never the primary
# `par-e2e-postgres` (par-run is empty there). The primary container is owned by
# the primary checkout's own teardown / `make e2e-clean`; reap is for crashed
# worktree agents. `--filter label=par-run` can't express "non-empty" (it
# matches the key even when the value is ""), so inspect each candidate.
ids=""
for id in $(docker ps -aq --filter label=par-e2e=1 || true); do
  run=$(docker inspect -f '{{ index .Config.Labels "par-run" }}' "$id" 2>/dev/null || true)
  [ -n "$run" ] && ids="$ids $id"
done
if [ -n "$ids" ]; then
  echo "▶ Removing $(echo $ids | wc -w | tr -d ' ') worktree test container(s)..."
  # shellcheck disable=SC2086
  docker rm -f $ids >/dev/null 2>&1 || true
else
  echo "  no worktree test containers to reap"
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
