#!/usr/bin/env bash
# Deploy HelixObs Grafana dashboards and datasources to the k8s cluster.
# Dashboards: each JSON file becomes a ConfigMap with label grafana_dashboard=1.
# Datasources: k8s/grafana/manifests/ applied directly (grafana_datasource=1 already set).
# Both are picked up by the Grafana sidecars automatically — no restart required.
#
# Usage: ./deploy-dashboards.sh [namespace]
# Default namespace: monitoring

set -euo pipefail

NAMESPACE=${1:-monitoring}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DASHBOARD_DIR="$(cd "${SCRIPT_DIR}/../grafana/dashboards" && pwd)"
MANIFESTS_DIR="${SCRIPT_DIR}/grafana/manifests"
FOLDER="HelixObs"

echo "Deploying datasources from ${MANIFESTS_DIR} to namespace ${NAMESPACE}..."

for yaml_file in "${MANIFESTS_DIR}"/*.yaml; do
  echo "  → $(basename "${yaml_file}")"
  kubectl apply -f "${yaml_file}"
done

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

echo "Done. Grafana sidecars will reload within 30 seconds."
