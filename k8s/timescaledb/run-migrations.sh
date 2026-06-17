#!/usr/bin/env bash
# Apply HelixObs TimescaleDB migrations to the k8s StatefulSet pod.
#
# Usage: ./run-migrations.sh [namespace]
# Default namespace: helixobs

set -euo pipefail

NAMESPACE=${1:-helixobs}
POD="timescaledb-0"
MIGRATIONS_DIR="$(cd "$(dirname "$0")/../../.." && pwd)/herald/migrations"

echo "Waiting for $POD to be ready..."
kubectl wait pod "$POD" -n "$NAMESPACE" --for=condition=Ready --timeout=120s

echo "Applying migrations from $MIGRATIONS_DIR..."

for sql_file in $(ls -1 "$MIGRATIONS_DIR"/*.sql | sort); do
  name=$(basename "$sql_file")
  echo "  → $name"
  kubectl exec -i -n "$NAMESPACE" "$POD" -- \
    psql -U helix -d helixobs -v ON_ERROR_STOP=1 < "$sql_file"
done

echo "Done."
