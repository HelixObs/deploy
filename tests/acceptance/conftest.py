"""
Acceptance test fixtures.

Requires the full Docker Compose stack to be running:
    cd deploy && docker compose up -d

Environment variables (all have localhost defaults):
    DB_URL        - TimescaleDB connection string
    GATEWAY_URL   - HTTP API base URL
    SHERLOCK_URL  - Sherlock service base URL
    TEMPO_URL     - Tempo HTTP API base URL
    PROMETHEUS_URL - Prometheus base URL
    OTLP_ENDPOINT - OTLP gRPC endpoint for the Python client
"""

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
GATEWAY_URL    = os.getenv("GATEWAY_URL",    "http://localhost:8080")
SHERLOCK_URL   = os.getenv("SHERLOCK_URL",   "http://localhost:8082")
TEMPO_URL      = os.getenv("TEMPO_URL",      "http://localhost:3201")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9091")
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
def gateway():
    return httpx.Client(base_url=GATEWAY_URL, timeout=30)


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
    """A real Instrument that exports spans to the running gateway via OTLP gRPC."""
    inst = Instrument.__new__(Instrument)
    inst.instrument_id = "TEST"

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
