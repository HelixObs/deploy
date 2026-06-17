#!/usr/bin/env bash
# Deploy HelixObs Grafana dashboards to the k8s cluster.
# Each JSON file becomes a ConfigMap with label grafana_dashboard=1,
# which the Grafana sidecar picks up automatically.
#
# Usage: ./deploy-dashboards.sh [namespace]
# Default namespace: monitoring

set -euo pipefail

NAMESPACE=${1:-monitoring}
DASHBOARD_DIR="$(cd "$(dirname "$0")/../grafana/dashboards" && pwd)"
FOLDER="HelixObs"

echo "Deploying dashboards from ${DASHBOARD_DIR} to namespace ${NAMESPACE}..."

for json_file in "${DASHBOARD_DIR}"/*.json; do
  name=$(basename "${json_file}" .json | tr '_' '-')
  cm_name="helixobs-dashboard-${name}"

  echo "  → ${cm_name}"

  kubectl create configmap "${cm_name}" \
    --from-file="${name}.json=${json_file}" \
    --namespace "${NAMESPACE}" \
    --dry-run=client -o yaml \
  | kubectl label --local -f - \
      grafana_dashboard=1 \
      k8s-sidecar-target-directory="${FOLDER}" \
      --dry-run=client -o yaml \
  | kubectl apply -f -
done

echo "Done. Grafana sidecar will reload dashboards within 30 seconds."
