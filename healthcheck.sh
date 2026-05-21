#!/usr/bin/env bash
# HelixObs stack healthcheck
# Usage: ./healthcheck.sh
set -euo pipefail

PASS=0; FAIL=0; WARN=0

ok()   { echo "  ✓ $*"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
warn() { echo "  ~ $*"; WARN=$((WARN+1)); }
header() { echo; echo "── $* ──────────────────────────────────────"; }

# ── Containers ────────────────────────────────────────────────────────────────
header "Containers"
EXPECTED=(db herald grafana loki mock-telescope node-exporter
          otel-collector prometheus sherlock tempo ui alloy)
for svc in "${EXPECTED[@]}"; do
  name="helixobs-${svc}-1"
  status=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || echo "missing")
  health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' "$name" 2>/dev/null || echo "missing")
  if [[ "$status" == "running" ]]; then
    if [[ "$health" == "healthy" || "$health" == "n/a" ]]; then
      ok "$svc ($health)"
    else
      warn "$svc running but health=$health"
    fi
  else
    fail "$svc — status=$status"
  fi
done

# ── Service endpoints ─────────────────────────────────────────────────────────
header "Service endpoints"

probe() {
  local label=$1 url=$2 expected=$3 timeout=${4:-5}
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$timeout" "$url" 2>/dev/null || true)
  if [[ "$code" == "$expected" ]]; then
    ok "$label ($url) → $code"
  else
    fail "$label ($url) → $code (expected $expected)"
  fi
}

probe "Herald metrics"      http://localhost:2112/metrics            200
probe "Herald API (404→ok)" http://localhost:8080/api/v1/entity/x/graph 404 30
probe "Sherlock health"      http://localhost:8082/health             200
probe "Sherlock metrics"     http://localhost:9102/metrics            200
probe "Prometheus UI"        http://localhost:9091/-/healthy          200
probe "Grafana"              http://localhost:3001/api/health         200
probe "UI"                   http://localhost:8081                    200
probe "Loki"                 http://localhost:3101/loki/api/v1/status/buildinfo 200
probe "Tempo"                http://localhost:3201/status             200

# ── Prometheus scrape targets ─────────────────────────────────────────────────
header "Prometheus scrape targets"
targets=$(curl -sf http://localhost:9091/api/v1/targets 2>/dev/null || echo "{}")
for job in herald sherlock loki otel-collector tempo node; do
  health=$(echo "$targets" | python3 -c "
import sys,json
data=json.load(sys.stdin).get('data',{}).get('activeTargets',[])
t=next((x for x in data if x['labels'].get('job')=='$job'),None)
print(t['health'] if t else 'missing')
" 2>/dev/null || echo "error")
  if [[ "$health" == "up" ]]; then
    ok "$job"
  else
    fail "$job — $health"
  fi
done

# ── Grafana datasources ───────────────────────────────────────────────────────
header "Grafana datasources"
ds=$(curl -sf http://admin:admin@localhost:3001/api/datasources 2>/dev/null || echo "[]")
for name in Prometheus Loki Tempo TimescaleDB; do
  found=$(echo "$ds" | python3 -c "
import sys,json
ds=json.load(sys.stdin)
print('yes' if any(d['name']=='$name' for d in ds) else 'no')
" 2>/dev/null || echo "error")
  if [[ "$found" == "yes" ]]; then
    ok "$name"
  else
    fail "$name datasource missing"
  fi
done

# ── Grafana dashboards ────────────────────────────────────────────────────────
header "Grafana dashboards"
boards=$(curl -sf "http://admin:admin@localhost:3001/api/search?type=dash-db" 2>/dev/null || echo "[]")
for uid in helix-platform-health helix-sherlock-cost helix-entity-inspector helix-error-entities; do
  found=$(echo "$boards" | python3 -c "
import sys,json
ds=json.load(sys.stdin)
print('yes' if any(d['uid']=='$uid' for d in ds) else 'no')
" 2>/dev/null || echo "error")
  if [[ "$found" == "yes" ]]; then
    ok "$uid"
  else
    fail "$uid dashboard missing"
  fi
done

# ── Database ──────────────────────────────────────────────────────────────────
header "Database"
run_sql() { docker exec helixobs-db-1 psql -U helix -d helixobs -t -c "$1" 2>/dev/null | tr -d ' \n'; }

# Tables present
for tbl in entities entity_events entity_operations operation_trace_seen sherlock_usage instrument_memory; do
  count=$(run_sql "SELECT COUNT(*) FROM $tbl;" 2>/dev/null || echo "error")
  if [[ "$count" =~ ^[0-9]+$ ]]; then
    ok "$tbl ($count rows)"
  else
    fail "$tbl — $count"
  fi
done

# Hypertables
ht_count=$(run_sql "SELECT COUNT(*) FROM timescaledb_information.hypertables;" 2>/dev/null || echo "0")
if [[ "$ht_count" -ge 4 ]]; then
  ok "$ht_count hypertables registered"
else
  warn "only $ht_count hypertables (expected ≥4)"
fi

# ── Herald metrics ───────────────────────────────────────────────────────────
header "Herald metrics"
gw=$(curl -sf http://localhost:2112/metrics 2>/dev/null || echo "")

check_metric() {
  local name=$1 src=${2:-gw}
  local haystack="${!src}"
  if echo "$haystack" | grep -q "${name}"; then
    ok "$name present"
  else
    fail "$name missing"
  fi
}

check_metric "helix_entities_total"
check_metric "helix_span_processing_duration_seconds"
check_metric "helix_parent_resolution_latency_seconds"
check_metric "helix_trace_store_lookup_duration_seconds"
check_metric "helix_db_writes_total"

entity_count=$(echo "$gw" | grep '^helix_entities_total{' | awk -F' ' '{sum+=$NF} END {printf "%d", sum}')
if [[ "$entity_count" -gt 0 ]]; then
  ok "entities processed: $entity_count spans"
else
  warn "no entity spans counted yet"
fi

# ── Sherlock metrics ──────────────────────────────────────────────────────────
header "Sherlock metrics"
sk=$(curl -sf http://localhost:9102/metrics 2>/dev/null || echo "")
for m in sherlock_queries_total sherlock_tokens_total sherlock_cost_usd_total \
          sherlock_tool_calls_total sherlock_cost_per_query_usd; do
  if echo "$sk" | grep -q "${m}"; then
    ok "$m present"
  else
    fail "$m missing"
  fi
done

# ── Pipeline activity ─────────────────────────────────────────────────────────
header "Pipeline activity (last 60s)"
recent=$(docker exec helixobs-db-1 psql -U helix -d helixobs -t -c \
  "SELECT COUNT(*) FROM entities WHERE created_at > now() - interval '60 seconds';" \
  2>/dev/null | tr -d ' \n' || echo "0")
if [[ "$recent" -gt 0 ]]; then
  ok "mock-telescope active — $recent entities in last 60s"
else
  warn "no new entities in last 60s — mock-telescope may be stalled"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════"
total=$((PASS + FAIL + WARN))
echo "  PASS $PASS  FAIL $FAIL  WARN $WARN  (of $total checks)"
echo "════════════════════════════════════════════"
[[ $FAIL -eq 0 ]]
