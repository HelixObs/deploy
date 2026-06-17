#!/usr/bin/env bash
# Deploy the full HelixObs stack to the k8s cluster.
#
# Prerequisites:
#   - kubectl configured pointing at the target cluster
#   - timescaledb-credentials Secret already applied (deploy/k8s/timescaledb/manifests/secret.yaml)
#   - herald-secrets and sherlock-secrets Secrets filled in with real values
#   - helixobs namespace exists (kubectl create namespace helixobs)
#
# Usage: ./deploy.sh [namespace]
# Default namespace: helixobs

set -euo pipefail

NAMESPACE=${1:-helixobs}
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying HelixObs to namespace: $NAMESPACE"

apply() {
  local component=$1
  echo ""
  echo "==> $component"
  kubectl apply -f "$DIR/$component/manifests/" -n "$NAMESPACE"
}

apply otel-collector
apply herald
apply sherlock
apply ui

echo ""
echo "Waiting for deployments to roll out..."
kubectl rollout status deployment/otel-collector -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/herald          -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/sherlock         -n "$NAMESPACE" --timeout=120s
kubectl rollout status deployment/ui               -n "$NAMESPACE" --timeout=120s

echo ""
echo "Done. Services:"
kubectl get svc -n "$NAMESPACE"
echo ""
echo "UI: https://helixobs.chimefrb.xyz  (cert issued by cert-manager within ~60s)"
echo "Herald OTLP gRPC: check 'kubectl get svc herald-otlp -n $NAMESPACE' for external IP"
