# deploy

Infrastructure and configuration for the full HelixObs stack. Everything needed to
run the platform locally or in CI lives here.

## What's here

```
docker-compose.yml          Full stack: DB, herald, OTel Collector, Sherlock, UI,
                            mock-telescope, Prometheus, Loki, Tempo, Grafana, Alloy, Caddy
Caddyfile                   Caddy reverse proxy config — TLS termination for UI, Grafana, gRPC
alloy-config.alloy          Grafana Alloy: scrapes stdout logs → Loki
otel-collector-config.yml   OTel Collector: receives OTLP → exports to Tempo + Loki
prometheus.yml              Prometheus scrape config (all services)
loki-config.yml             Loki storage and ingestion config
tempo-config.yml            Tempo storage config (5-min max block, 1 GB mem limit)
instruments/
  example-instrument.yml.template  Template — copy to <id>-context.yml, fill in values, do not commit
  .gitignore                       Ignores all *.yml (actual configs stay off of git)
grafana/
  provisioning/
    datasources/            Auto-provisions Prometheus, Loki, Tempo datasources
    dashboards/             Auto-provisions dashboard JSON files from grafana/dashboards/
  dashboards/
    platform_health.json    HelixObs Platform Health (Host + Herald + Backends rows)
    sherlock_cost.json      Sherlock Cost (token usage, cost, latency, tool breakdown)
    entity_inspector.json   Entity Inspector (provenance graph, events timeline)
    error_entities.json     Error Entities (table of entities with helix.error)
tests/
  acceptance/               T1 acceptance test suite (see tests/acceptance/AGENT.md)
.github/
  workflows/
    acceptance.yml          PR + push to main: non-Sherlock T1 tests
    acceptance-nightly.yml  Nightly: full T1 including Sherlock (requires ANTHROPIC_API_KEY)
```

## Services and ports

### Production (Caddy-proxied, externally accessible)

| Service | External port | Notes |
|---|---|---|
| UI | 443 (HTTPS) | Caddy → ui:3000 |
| Grafana | 3001 (HTTPS) | Caddy → grafana:3000; `admin` / `admin` |
| Herald (OTLP gRPC) | 4317 (plaintext, Phase 1) | Direct until CHIME switches to TLS |
| OTel Collector (gRPC logs) | 4319 (plaintext, Phase 1) | Direct until Phase 2 |

### Internal / SSH-tunnel only (no Arbutus security group rule)

| Service | Host port | Notes |
|---|---|---|
| Herald (API) | 8080 | `GET /api/v1/entity/{id}/graph` |
| Herald (metrics) | 2112 | Prometheus scrape target |
| Sherlock | — | Docker-internal only; no host binding |
| Sherlock (metrics) | 9102 | Prometheus scrape target |
| Prometheus | 9091 | Query UI |
| TimescaleDB | 5432 | `helix` / `helix` / `helixobs` |
| Loki | 3101 | Log ingestion and query |
| Tempo | 3201 | Trace query (Grafana datasource) |

## TLS (Caddy) migration

**Phase 1 (current):** Caddy handles HTTPS for UI (443) and Grafana (3001). CHIME still connects
plaintext on 4317/4319. No CHIME-side changes needed.

**Phase 2 (gRPC TLS):** Coordinate with CHIME to update their OTLP endpoint to TLS.
1. Uncomment the gRPC blocks in `Caddyfile`.
2. Remove `"4317:4317"` from `herald` ports in `docker-compose.yml`.
3. Remove `"4319:4317"` from `otel-collector` ports in `docker-compose.yml`.
4. Add `"4317:4317"` and `"4319:4319"` to `caddy` ports in `docker-compose.yml`.
5. `docker compose up -d caddy herald otel-collector`
6. CHIME updates `HERALD_ENDPOINT=206-12-91-148.cloud.computecanada.ca:4317` (TLS, no `insecure=True`).
7. Close plaintext 4317/4319 in Arbutus security group (already restricted to CHIME's IP range).

**Production env vars** (set in shell or `/opt/helixobs/.env`):
```
HELIXOBS_DOMAIN=206-12-91-148.cloud.computecanada.ca
UI_BASE_URL=https://206-12-91-148.cloud.computecanada.ca
GRAFANA_URL=https://206-12-91-148.cloud.computecanada.ca:3001
```

## Dashboards

Grafana dashboards are provisioned automatically from `grafana/dashboards/`. Changes to
JSON files take effect immediately in a running stack — no restart needed (Grafana watches
the provisioning directory).

Each panel has a `description` field visible as an info tooltip in Grafana.

### platform_health.json
Four rows: Host (CPU/memory/disk/network), Herald (ingestion rate, latency, parent
resolution, DB writes, RSS, trace store, connection pool, error rate, resolution latency,
store lookup latency), Backends (herald uptime, OTel Collector, Loki, Tempo).

### sherlock_cost.json
Stat row (cost, queries, success rate, duration, tokens), timeseries rows (cumulative cost,
query latency by type, tool calls/s, tool latency p95, tool success rate, token usage,
cost-per-query distribution), cost ledger table from TimescaleDB.

## Database migrations

Migrations run automatically on first container start via `docker-entrypoint-initdb.d/`.
They are mounted read-only from `../herald/migrations/`. To add a new migration:

1. Create `herald/migrations/00N_description.sql`.
2. Add a mount line in `docker-compose.yml` under the `db` service.
3. The migration runs on the next `docker compose up` with a fresh volume (`down -v`).

## Running the stack

```bash
# Production (with Caddy TLS — set env vars first):
export HELIXOBS_DOMAIN=206-12-91-148.cloud.computecanada.ca
export UI_BASE_URL=https://206-12-91-148.cloud.computecanada.ca
export GRAFANA_URL=https://206-12-91-148.cloud.computecanada.ca:3001
docker compose up --build

# Local dev (no TLS — services accessible on localhost ports via SSH tunnel or direct):
docker compose up --build
# UI:     http://localhost — Caddy issues local CA cert for 'localhost'
# Grafana: https://localhost:3001 (Caddy local CA)
# Herald: localhost:4317 (plaintext gRPC, unchanged for dev)

# Caddy only (after code change):
docker compose up -d caddy

# Rebuild only one service:
docker compose build herald && docker compose up -d herald

# Tail logs:
docker compose logs -f herald sherlock caddy

# Verify Caddy cert issuance:
docker compose logs caddy | grep -i "certificate\|tls\|acme"

# Tear down (removes volumes — resets DB):
docker compose down -v
```

## Environment variables for CI/local

| Variable | Required for | Notes |
|---|---|---|
| `HELIXOBS_DOMAIN` | Caddy TLS cert | Defaults to `localhost` (local CA cert) |
| `UI_BASE_URL` | Herald notification links | Full URL to UI; defaults to `http://localhost:8081` |
| `GRAFANA_URL` | Herald + Sherlock links | Full URL to Grafana; defaults to `http://localhost:3001` |
| `ANTHROPIC_API_KEY` | Sherlock agent loop | Use a placeholder for non-Sherlock CI |
| `GITHUB_TOKEN` | `fetch_github_*` tools | Optional — Sherlock works without it |

## Optional remote forwarding

Set these in `.env` on the production host to forward signals to an additional backend.
When unset the stack operates entirely locally — all variables are optional.

| Variable | Service | Description |
|---|---|---|
| `REMOTE_TEMPO_ENDPOINT` | OTel Collector | OTLP HTTP endpoint for remote trace storage (e.g. `https://tempo.example.com`) |
| `REMOTE_LOKI_ENDPOINT` | OTel Collector | OTLP HTTP endpoint for remote log storage — OTLP path (e.g. `https://loki.example.com`) |
| `REMOTE_LOKI_TENANT_ID` | OTel Collector + Alloy | `X-Scope-OrgID` / tenant ID sent with remote Loki pushes |
| `REMOTE_LOKI_URL` | Alloy | Loki push API URL for sidecar-collected logs (e.g. `https://loki.example.com/loki/api/v1/push`) |
| `REMOTE_METRICS_URL` | Prometheus | Remote write URL for metrics (e.g. Mimir). Uncomment `remote_write` in `prometheus.yml` to enable. |

## Adding a new service

1. Add a service block in `docker-compose.yml` on the `helix` network.
2. Add a Prometheus scrape job in `prometheus.yml`.
3. Add a Grafana datasource if needed under `grafana/provisioning/datasources/`.
4. Add relevant panels to an existing dashboard or create a new JSON under `grafana/dashboards/`.
