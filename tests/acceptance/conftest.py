"""
Acceptance test fixtures.

Requires the full Docker Compose stack to be running:
    cd deploy && docker compose up -d

Environment variables (all have localhost defaults):
    DB_URL        - TimescaleDB connection string
    HERALD_URL   - HTTP API base URL
    SHERLOCK_URL  - Sherlock service base URL
    TEMPO_URL     - Tempo HTTP API base URL
    PROMETHEUS_URL - Prometheus base URL
    LOKI_URL      - Loki HTTP API base URL
    LOG_ENDPOINT  - OTel Collector gRPC for OTLP log shipping (port 4319)
    OTLP_ENDPOINT - OTLP gRPC endpoint for the Python client
"""

import json
import os
import time
import uuid

import psycopg2
import psycopg2.extras
import pytest
import httpx
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

from helixobs.instrument import Instrument

# ── Config ────────────────────────────────────────────────────────────────────

DB_URL         = os.getenv("DB_URL",         "postgres://helix:helix@localhost:5432/helixobs")
HERALD_URL    = os.getenv("HERALD_URL",    "http://localhost:8080")
SHERLOCK_URL   = os.getenv("SHERLOCK_URL",   "http://localhost:8082")
TEMPO_URL      = os.getenv("TEMPO_URL",      "http://localhost:3201")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9091")
LOKI_URL       = os.getenv("LOKI_URL",       "http://localhost:3101")
LOG_ENDPOINT   = os.getenv("LOG_ENDPOINT",   "http://localhost:4319")
OTLP_ENDPOINT  = os.getenv("OTLP_ENDPOINT", "localhost:4317")


# ── DB fixture ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def db():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def db_cursor(db):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        yield cur


# ── HTTP clients ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def herald():
    return httpx.Client(base_url=HERALD_URL, timeout=30)


@pytest.fixture(scope="session")
def sherlock():
    return httpx.Client(base_url=SHERLOCK_URL, timeout=300)


@pytest.fixture(scope="session")
def prometheus():
    return httpx.Client(base_url=PROMETHEUS_URL, timeout=10)


@pytest.fixture(scope="session")
def tempo_client():
    return httpx.Client(base_url=TEMPO_URL, timeout=10)


# ── Instrument fixture ────────────────────────────────────────────────────────

@pytest.fixture
def instrument():
    """A real Instrument that exports spans to the running herald via OTLP gRPC."""
    inst = Instrument.__new__(Instrument)
    inst.instrument_id = "TEST"
    inst._process_name = None

    from helixobs._store import TraceStore
    inst._store = TraceStore()

    resource = Resource.create({
        "service.name": "acceptance-tests",
        "helix.instrument.id": "TEST",
    })
    exporter = OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    inst._tracer = provider.get_tracer("helixobs")
    inst._provider = provider
    yield inst
    provider.force_flush(timeout_millis=5000)
    provider.shutdown()


# ── Helpers ───────────────────────────────────────────────────────────────────

def unique_id(prefix: str = "e") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def emit_entity(instrument, entity_id: str, parents: list[str] | None = None, events: list[dict] | None = None) -> str:
    """Emit a single entity span and flush. Returns entity_id."""
    token = instrument.create("test-stage", id=entity_id, parents=parents or []).start()
    if events:
        for ev in events:
            token.add_event(ev["name"], ev.get("attrs", {}))
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)
    return entity_id


def wait_for_entity(db_cursor, entity_id: str, timeout: float = 10.0) -> dict | None:
    """Poll TimescaleDB until the entity row appears or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db_cursor.execute("SELECT * FROM entities WHERE id = %s ORDER BY created_at DESC LIMIT 1", (entity_id,))
        row = db_cursor.fetchone()
        if row:
            return dict(row)
        time.sleep(0.3)
    return None


def wait_for_operation(db_cursor, entity_id: str, timeout: float = 10.0) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db_cursor.execute(
            "SELECT * FROM entity_operations WHERE entity_id = %s ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        )
        row = db_cursor.fetchone()
        if row:
            return dict(row)
        time.sleep(0.3)
    return None


def wait_for_events(db_cursor, entity_id: str, count: int = 1, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db_cursor.execute(
            "SELECT * FROM entity_events WHERE entity_id = %s ORDER BY timestamp_ns ASC",
            (entity_id,),
        )
        rows = db_cursor.fetchall()
        if len(rows) >= count:
            return [dict(r) for r in rows]
        time.sleep(0.3)
    return []


def wait_for_loki(stream_selector: str, line_filter: str = "", timeout: float = 20.0) -> list[dict]:
    """Poll Loki until matching log lines appear. Returns list of parsed JSON log bodies.

    Args:
        stream_selector: LogQL stream selector, e.g. '{service_name="my-svc"}'
        line_filter:     Optional LogQL pipe filter, e.g. '| helix_entity_id = `abc`'
        timeout:         Seconds to wait (Loki OTLP ingestion lag is 2-5 s).
    """
    query = f"{stream_selector} {line_filter}".strip()
    client = httpx.Client(base_url=LOKI_URL, timeout=10)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/loki/api/v1/query_range", params={
                "query": query,
                "limit": 50,
                "start": str(int((time.time() - 120) * 1e9)),
            })
            if resp.status_code == 200:
                lines = []
                for stream in resp.json().get("data", {}).get("result", []):
                    for _ts, msg in stream.get("values", []):
                        try:
                            lines.append(json.loads(msg))
                        except Exception:
                            lines.append({"_raw": msg})
                if lines:
                    return lines
        except Exception:
            pass
        time.sleep(2)
    return []
