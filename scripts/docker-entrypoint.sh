#!/usr/bin/env bash
# Container bootstrap for standalone runs: apply migrations, then hand off to CMD.
# Idempotent. Disable auto-migration with AUTO_MIGRATE=false (e.g. multi-replica
# deploys where a separate job runs migrations).
set -euo pipefail

if [ "${AUTO_MIGRATE:-true}" != "false" ]; then
  echo "[entrypoint] applying migrations (alembic upgrade head)"
  migrated=0
  for attempt in $(seq 1 15); do
    if alembic upgrade head; then
      migrated=1
      break
    fi
    echo "[entrypoint] database not ready (attempt ${attempt}/15) — retrying in 2s"
    sleep 2
  done
  if [ "${migrated}" != "1" ]; then
    echo "[entrypoint] migrations failed after retries — aborting" >&2
    exit 1
  fi
else
  echo "[entrypoint] AUTO_MIGRATE=false — skipping migrations"
fi

echo "[entrypoint] starting: $*"
exec "$@"
