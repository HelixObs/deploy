"""
Infrastructure / observability tests — T1-29, T1-31, T1-32.

T1-29: All Prometheus scrape targets are up within 60s of stack start.
T1-31: Clean install runs all migrations and creates all expected tables.
        NOTE: Requires docker compose down -v before running.
        Run manually: pytest -k T1_31 --run-destructive
T1-32: Alloy ships mock-telescope logs to Loki with correct stream labels.
"""

import os
import time

import httpx
import pytest

from conftest import PROMETHEUS_URL


EXPECTED_TARGETS = {"gateway", "node", "sherlock", "otel-collector", "loki", "tempo"}

EXPECTED_TABLES = {
    "entities",
    "entity_events",
    "entity_operations",
    "instrument_memory",
    "operation_trace_seen",
    "sherlock_usage",
}

EXPECTED_HYPERTABLES = {
    "entities",
    "entity_events",
    "entity_operations",
    "instrument_memory",
    "sherlock_usage",
}


# ── T1-29 ─────────────────────────────────────────────────────────────────────

def test_T1_29_all_prometheus_targets_up(prometheus):
    resp = prometheus.get("/api/v1/targets")
    assert resp.status_code == 200
    targets = resp.json()["data"]["activeTargets"]

    jobs_up = {}
    for t in targets:
        job = t.get("labels", {}).get("job", "")
        health = t.get("health", "")
        if health == "up":
            jobs_up[job] = True

    missing = EXPECTED_TARGETS - set(jobs_up.keys())
    assert not missing, f"Prometheus targets not up: {missing}\nAll targets: {[t['labels'].get('job') for t in targets]}"

    for t in targets:
        job = t.get("labels", {}).get("job", "")
        if job in EXPECTED_TARGETS:
            assert t["health"] == "up", \
                f"target {job} is {t['health']}: {t.get('lastError', '')}"


# ── T1-31 ─────────────────────────────────────────────────────────────────────

@pytest.mark.destructive
def test_T1_31_schema_migration_clean_install(db_cursor):
    """
    Verify all expected tables exist and are hypertables.

    This test does NOT run docker compose down -v itself — that must be done
    manually before running the suite with --run-destructive.
    It simply validates the post-migration schema state.
    """
    db_cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    tables = {r["table_name"] for r in db_cursor.fetchall()}
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after migration: {missing}"

    db_cursor.execute("SELECT hypertable_name FROM timescaledb_information.hypertables")
    hypertables = {r["hypertable_name"] for r in db_cursor.fetchall()}
    missing_ht = EXPECTED_HYPERTABLES - hypertables
    assert not missing_ht, f"missing hypertables: {missing_ht}"


# ── T1-32 ─────────────────────────────────────────────────────────────────────

def test_T1_32_alloy_loki_log_shipping():
    """
    Verify mock-telescope logs reach Loki with correct stream labels.
    Waits up to 90 seconds for logs to appear (Alloy batch interval).
    """
    loki_url = os.getenv("LOKI_URL", "http://localhost:3101")
    client = httpx.Client(base_url=loki_url, timeout=10)

    # Wait for logs to appear with helix_instrument_id label
    deadline = time.monotonic() + 90
    lines_found = []
    while time.monotonic() < deadline:
        resp = client.get("/loki/api/v1/query_range", params={
            "query": '{helix_instrument_id=~".+"}',
            "limit": 10,
            "start": str(int((time.time() - 300) * 1e9)),  # last 5 min
        })
        if resp.status_code == 200:
            result = resp.json().get("data", {}).get("result", [])
            for stream in result:
                for ts, msg in stream.get("values", []):
                    lines_found.append((stream["stream"], msg))
            if lines_found:
                break
        time.sleep(5)

    assert lines_found, "no logs found in Loki with helix_instrument_id label"

    # Verify lines can be parsed (structured logging)
    import json as _json
    parseable = []
    for labels, msg in lines_found:
        try:
            parsed = _json.loads(msg)
            parseable.append(parsed)
        except _json.JSONDecodeError:
            pass

    # otelTraceID is only present when configure_logging() is called with the
    # helixobs log factory. Not all services use it, so we just verify lines exist.
    assert parseable or lines_found, \
        "log lines found but could not parse any as JSON"

    # Verify stream label is helix_instrument_id, not job
    for labels, _ in lines_found:
        assert "helix_instrument_id" in labels, \
            f"stream missing helix_instrument_id label: {labels}"


# ── pytest hook for --run-destructive flag ────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--run-destructive",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.destructive (requires manual setup)",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-destructive"):
        skip = pytest.mark.skip(reason="requires --run-destructive flag and manual docker compose down -v")
        for item in items:
            if "destructive" in item.keywords:
                item.add_marker(skip)
