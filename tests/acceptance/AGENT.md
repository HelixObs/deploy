# tests/acceptance

T1 acceptance tests for the full HelixObs stack. Tests run against a live Docker Compose
stack — no mocks. Each test exercises a real code path end-to-end.

## How to run

```bash
# Start the stack first:
docker compose -f ../../docker-compose.yml up -d --build

# All non-Sherlock, non-destructive tests (CI default):
pytest -v -m "not sherlock and not destructive"

# Full T1 including Sherlock (requires ANTHROPIC_API_KEY):
pytest -v -m "not destructive"

# Destructive tests only (run after docker compose down -v):
pytest -v -m "destructive"
```

## Required environment variables

| Variable | Default | Used by |
|---|---|---|
| `DB_URL` | `postgres://helix:helix@localhost:5432/helixobs` | DB fixture |
| `HERALD_URL` | `http://localhost:8080` | herald fixture |
| `SHERLOCK_URL` | `http://localhost:8082` | sherlock fixture |
| `PROMETHEUS_URL` | `http://localhost:9091` | prometheus fixture |
| `TEMPO_URL` | `http://localhost:3201` | infra tests |
| `LOKI_URL` | `http://localhost:3101` | infra tests |
| `OTLP_ENDPOINT` | `localhost:4317` | instrument fixture |
| `ANTHROPIC_API_KEY` | — | sherlock-marked tests only |

## Test files

| File | Marker | Tests |
|---|---|---|
| `test_herald.py` | (none) | T1-01 to T1-15: entity ingestion, events, operations, dedup, metrics |
| `test_operations.py` | (none) | T1-17 to T1-19: entity_operations write path, placeholder upsert, dedup |
| `test_sherlock.py` | `sherlock` | T1-20 to T1-24: streaming protocol, memory, reply endpoint, memory CRUD |
| `test_regression.py` | mixed | T1-33 to T1-38: operation_trace_seen, graph API, CTE regression, Sherlock |
| `test_infra.py` | mixed | T1-29, T1-31 (destructive), T1-32: Prometheus targets, schema, Loki logs |

## Fixtures (conftest.py)

| Fixture | Scope | Description |
|---|---|---|
| `db_cursor` | session | psycopg2 cursor connected to TimescaleDB |
| `herald_client` | session | `requests.Session` pointed at herald API |
| `sherlock_client` | session | `requests.Session` pointed at Sherlock |
| `prometheus_client` | session | `requests.Session` pointed at Prometheus |
| `instrument` | function | Real `Instrument` instance exporting to herald via OTLP gRPC |

Helper functions in `conftest.py`:
- `unique_id(prefix)` — generates a `prefix-<uuid>` string
- `emit_entity(instrument, stage, ...)` — emits one entity span and flushes
- `wait_for_entity(cursor, entity_id, timeout=10)` — polls DB until row appears
- `wait_for_operation(cursor, entity_id, operation, timeout=10)` — polls entity_operations
- `wait_for_events(cursor, entity_id, event_name, timeout=10)` — polls entity_events

## Pytest marks

| Mark | Meaning |
|---|---|
| `sherlock` | Calls the Claude API — needs `ANTHROPIC_API_KEY`, skipped in PR CI |
| `destructive` | Drops/recreates schema — must run on a fresh stack (`down -v` first) |

## Key design notes

- Tests that check Prometheus counters query the herald's `/metrics` endpoint directly
  (not Prometheus) to avoid the 15-second scrape lag.
- Tests that check DB state after emitting spans always call `wait_for_entity()` first —
  the herald writes asynchronously in goroutines.
- The `instrument` fixture uses `BatchSpanProcessor` (same as production) with a short
  `schedule_delay_millis=100` to keep test latency low without polling.
