# deploy

Infrastructure and configuration for the full HelixObs stack. Everything needed to
run the platform locally or in CI lives here.

## What's here

```
docker-compose.yml          Full stack: DB, gateway, OTel Collector, Sherlock, UI,
                            mock-telescope, Prometheus, Loki, Tempo, Grafana, Alloy
alloy-config.alloy          Grafana Alloy: scrapes stdout logs → Loki
otel-collector-config.yml   OTel Collector: receives OTLP → exports to Tempo + Loki
prometheus.yml              Prometheus scrape config (all services)
loki-config.yml             Loki storage and ingestion config
tempo-config.yml            Tempo storage config (5-min max block, 1 GB mem limit)
grafana/
  provisioning/
    datasources/            Auto-provisions Prometheus, Loki, Tempo datasources
    dashboards/             Auto-provisions dashboard JSON files from grafana/dashboards/
  dashboards/
    platform_health.json    HelixObs Platform Health (Host + Gateway + Backends rows)
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

| Service | Port | Notes |
|---|---|---|
| Gateway (OTLP gRPC) | 4317 | Instrument OTLP endpoint |
| Gateway (API) | 8080 | `GET /api/v1/entity/{id}/graph` |
| Gateway (metrics) | 2112 | Prometheus scrape target |
| Sherlock | 8082 | FastAPI — diagnose, memory, health |
| Sherlock (metrics) | 9102 | Prometheus scrape target |
| UI | 8081 | Next.js front end |
| Grafana | 3001 | `admin` / `admin` |
| Prometheus | 9091 | Query UI |
| TimescaleDB | 5432 | `helix` / `helix` / `helixobs` |
| Loki | 3101 | Log ingestion and query |
| Tempo | 3201 | Trace query (Grafana datasource) |

## Dashboards

Grafana dashboards are provisioned automatically from `grafana/dashboards/`. Changes to
JSON files take effect immediately in a running stack — no restart needed (Grafana watches
the provisioning directory).

Each panel has a `description` field visible as an info tooltip in Grafana.

### platform_health.json
Four rows: Host (CPU/memory/disk/network), Gateway (ingestion rate, latency, parent
resolution, DB writes, RSS, trace store, connection pool, error rate, resolution latency,
store lookup latency), Backends (gateway uptime, OTel Collector, Loki, Tempo).

### sherlock_cost.json
Stat row (cost, queries, success rate, duration, tokens), timeseries rows (cumulative cost,
query latency by type, tool calls/s, tool latency p95, tool success rate, token usage,
cost-per-query distribution), cost ledger table from TimescaleDB.

## Database migrations

Migrations run automatically on first container start via `docker-entrypoint-initdb.d/`.
They are mounted read-only from `../gateway/migrations/`. To add a new migration:

1. Create `gateway/migrations/00N_description.sql`.
2. Add a mount line in `docker-compose.yml` under the `db` service.
3. The migration runs on the next `docker compose up` with a fresh volume (`down -v`).

## Running the stack

```bash
# Full stack (rebuilds images):
docker compose up --build

# Rebuild only one service:
docker compose build gateway && docker compose up -d gateway

# Tail logs:
docker compose logs -f gateway sherlock

# Tear down (removes volumes — resets DB):
docker compose down -v
```

## Environment variables for CI/local

| Variable | Required for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sherlock agent loop | Use a placeholder for non-Sherlock CI |
| `GITHUB_TOKEN` | `fetch_github_*` tools | Optional — Sherlock works without it |

## Adding a new service

1. Add a service block in `docker-compose.yml` on the `helix` network.
2. Add a Prometheus scrape job in `prometheus.yml`.
3. Add a Grafana datasource if needed under `grafana/provisioning/datasources/`.
4. Add relevant panels to an existing dashboard or create a new JSON under `grafana/dashboards/`.
