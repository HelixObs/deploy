"""
Gateway + DB correctness tests — T1-01 through T1-15.

Requires: full Docker Compose stack running, OTLP gateway reachable at localhost:4317.
"""

import time

import pytest

from conftest import emit_entity, unique_id, wait_for_entity, wait_for_events


# ── T1-01 ─────────────────────────────────────────────────────────────────────

def test_T1_01_root_entity_creation(instrument, db_cursor):
    eid = unique_id("block")
    emit_entity(instrument, eid)
    row = wait_for_entity(db_cursor, eid)
    assert row is not None, f"entity {eid} not found in DB"
    assert row["instrument_id"] == "TEST"
    assert row["parent_ids"] == []
    assert row["trace_id"] is not None and row["trace_id"] != ""


# ── T1-02 ─────────────────────────────────────────────────────────────────────

def test_T1_02_derived_entity_single_parent(instrument, db_cursor):
    block_id = unique_id("block")
    cand_id  = unique_id("cand")
    emit_entity(instrument, block_id)
    emit_entity(instrument, cand_id, parents=[block_id])

    block_row = wait_for_entity(db_cursor, block_id)
    cand_row  = wait_for_entity(db_cursor, cand_id)
    assert block_row is not None
    assert cand_row is not None
    assert block_id in cand_row["parent_ids"]


# ── T1-03 ─────────────────────────────────────────────────────────────────────

def test_T1_03_n_to_1_provenance(instrument, db_cursor):
    block_id  = unique_id("block")
    cand_ids  = [unique_id("cand") for _ in range(5)]
    event_id  = unique_id("event")

    emit_entity(instrument, block_id)
    for cid in cand_ids:
        emit_entity(instrument, cid, parents=[block_id])
    emit_entity(instrument, event_id, parents=cand_ids)

    event_row = wait_for_entity(db_cursor, event_id)
    assert event_row is not None
    for cid in cand_ids:
        assert cid in event_row["parent_ids"], f"missing parent {cid}"

    # Recursive CTE should return 7 distinct nodes: 1 event + 5 cands + 1 block
    db_cursor.execute("""
        WITH RECURSIVE ancestors AS (
          SELECT id, parent_ids, 0 AS depth FROM entities WHERE id = %s
          UNION
          SELECT e.id, e.parent_ids, a.depth + 1
          FROM entities e JOIN ancestors a ON e.id = ANY(a.parent_ids)
          WHERE a.depth < 10
        )
        SELECT DISTINCT id, depth FROM ancestors ORDER BY depth
    """, (event_id,))
    rows = db_cursor.fetchall()
    assert len(rows) == 7, f"expected 7 ancestor rows, got {len(rows)}: {[r['id'] for r in rows]}"


# ── T1-04 ─────────────────────────────────────────────────────────────────────

def test_T1_04_provenance_dag_query_correctness(instrument, db_cursor):
    block_id = unique_id("block")
    cand1_id = unique_id("cand")
    cand2_id = unique_id("cand")
    event_id = unique_id("event")

    emit_entity(instrument, block_id)
    emit_entity(instrument, cand1_id, parents=[block_id])
    emit_entity(instrument, cand2_id, parents=[block_id])
    emit_entity(instrument, event_id, parents=[cand1_id, cand2_id])
    wait_for_entity(db_cursor, event_id)

    db_cursor.execute("""
        WITH RECURSIVE ancestors AS (
          SELECT id, parent_ids, 0 AS depth FROM entities WHERE id = %s
          UNION ALL
          SELECT e.id, e.parent_ids, a.depth + 1
          FROM entities e JOIN ancestors a ON e.id = ANY(a.parent_ids)
          WHERE a.depth < 10
        )
        SELECT id, depth FROM ancestors ORDER BY depth, id
    """, (event_id,))
    rows = {r["id"]: r["depth"] for r in db_cursor.fetchall()}

    assert rows.get(event_id) == 0
    assert rows.get(cand1_id) == 1
    assert rows.get(cand2_id) == 1
    assert rows.get(block_id) == 2
    assert len(rows) == 4


# ── T1-05 ─────────────────────────────────────────────────────────────────────

def test_T1_05_gateway_transparency_non_helix_spans(instrument, db_cursor):
    # Emit a plain OTel span with no helix.* attributes
    # Capture the trace_id so we can verify no entity row was created for it
    tracer = instrument._tracer
    with tracer.start_as_current_span("plain-span") as span:
        trace_id_hex = format(span.get_span_context().trace_id, "032x")
    instrument._provider.force_flush(timeout_millis=3000)
    time.sleep(1)

    # No entity row should reference this trace_id
    db_cursor.execute("SELECT COUNT(*) AS n FROM entities WHERE trace_id = %s", (trace_id_hex,))
    assert db_cursor.fetchone()["n"] == 0, "non-helix span created an entity row"


# ── T1-06 ─────────────────────────────────────────────────────────────────────

def test_T1_06_helix_error_event_extraction(instrument, db_cursor):
    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    token.add_event("helix.error", {"type": "CLASSIFIER_TIMEOUT", "rack": "gpu-rack-3"})
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    events = wait_for_events(db_cursor, eid, count=1)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_name"] == "helix.error"
    assert ev["metadata"].get("type") == "CLASSIFIER_TIMEOUT"
    assert ev["metadata"].get("rack") == "gpu-rack-3"
    # Timestamp must be populated and non-zero
    assert ev["timestamp_ns"] > 0


# ── T1-07 ─────────────────────────────────────────────────────────────────────

def test_T1_07_helix_event_extraction(instrument, db_cursor):
    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    token.add_event("helix.event.rfi_flagged", {"fraction_flagged": "0.92", "algorithm": "spectral_kurtosis"})
    token.add_event("helix.event.candidate_promoted", {"snr": "23.4", "confidence": "0.97"})
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    events = wait_for_events(db_cursor, eid, count=2)
    names = {e["event_name"] for e in events}
    assert "helix.event.rfi_flagged" in names
    assert "helix.event.candidate_promoted" in names

    rfi = next(e for e in events if e["event_name"] == "helix.event.rfi_flagged")
    assert rfi["metadata"].get("algorithm") == "spectral_kurtosis"


# ── T1-08 ─────────────────────────────────────────────────────────────────────

def test_T1_08_non_helix_events_ignored(instrument, db_cursor):
    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    token._span.add_event("exception", {"exception.message": "test error"})
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)
    time.sleep(1)

    db_cursor.execute("SELECT COUNT(*) AS n FROM entity_events WHERE entity_id = %s", (eid,))
    assert db_cursor.fetchone()["n"] == 0


# ── T1-09 ─────────────────────────────────────────────────────────────────────

def test_T1_09_event_timestamp_correctness(instrument, db_cursor):
    import time as _time
    from opentelemetry import trace as otel_trace

    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    span_start_ns = token._span.start_time

    # Sleep 2 seconds then add the event
    _time.sleep(2)
    event_time_approx = _time.time_ns()
    token.add_event("helix.error", {"msg": "timestamp-test"})
    _time.sleep(0.5)
    span_end_approx = _time.time_ns()
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    events = wait_for_events(db_cursor, eid, count=1)
    assert len(events) == 1
    ts = events[0]["timestamp_ns"]
    tolerance_ns = 500_000_000  # 500ms

    assert abs(ts - event_time_approx) < tolerance_ns, \
        f"event timestamp {ts} not within 500ms of emit time {event_time_approx}"
    assert abs(ts - span_start_ns) > 1_500_000_000, \
        "event timestamp too close to span start (should be ~2s later)"


# ── T1-10 ─────────────────────────────────────────────────────────────────────

def test_T1_10_out_of_order_parent_arrives_later(instrument, db_cursor):
    block_id = unique_id("block")
    cand_id  = unique_id("cand")

    # Emit child first — block_id not yet in local TraceStore so goes as helix.parent.ids
    # We use a fresh instrument store to simulate cross-process (block_id not in store)
    from helixobs._store import TraceStore
    instrument._store = TraceStore()

    token = instrument.create("test-stage", id=cand_id, parents=[block_id]).start()
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    # Now emit the parent
    emit_entity(instrument, block_id)

    cand_row = wait_for_entity(db_cursor, cand_id)
    assert cand_row is not None
    assert block_id in cand_row["parent_ids"]


# ── T1-11 ─────────────────────────────────────────────────────────────────────

def test_T1_11_out_of_order_parent_never_arrives(instrument, db_cursor, prometheus):
    missing_parent = unique_id("missing")
    cand_id = unique_id("cand")

    resp = prometheus.get("/api/v1/query", params={"query": 'helix_parent_resolution_failed_total{instrument_id="TEST"}'})
    before = float(resp.json()["data"]["result"][0]["value"][1]) if resp.json()["data"]["result"] else 0.0

    token = instrument.create("test-stage", id=cand_id, parents=[missing_parent]).start()
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    # Entity should still land in DB even without parent resolution
    cand_row = wait_for_entity(db_cursor, cand_id)
    assert cand_row is not None
    # parent_ids attribute still stored
    assert missing_parent in cand_row["parent_ids"]


# ── T1-12 ─────────────────────────────────────────────────────────────────────

def test_T1_12_idempotency_duplicate_span(instrument, db_cursor):
    eid = unique_id("block")
    emit_entity(instrument, eid)
    time.sleep(2)  # ensure first write completes before second arrives
    emit_entity(instrument, eid)  # duplicate
    time.sleep(1)

    db_cursor.execute("SELECT COUNT(*) AS n FROM entities WHERE id = %s", (eid,))
    count = db_cursor.fetchone()["n"]
    assert count == 1, f"expected 1 row, got {count} (idempotency failed)"


# ── T1-13 ─────────────────────────────────────────────────────────────────────

def test_T1_13_prometheus_entity_counter(instrument, db_cursor):
    import httpx
    from conftest import HERALD_URL

    # Query gateway's own /metrics endpoint (always current, no scrape delay)
    metrics_url = HERALD_URL.replace(":8080", ":2112")
    def sum_metric(text, name, must_contain):
        total = 0.0
        for line in text.splitlines():
            if line.startswith(name) and must_contain in line and not line.startswith("#"):
                total += float(line.split()[-1])
        return total

    resp = httpx.get(f"{metrics_url}/metrics", timeout=5)
    before = sum_metric(resp.text, "helix_entities_total", 'instrument_id="TEST"')

    for _ in range(5):
        emit_entity(instrument, unique_id("e"))
    time.sleep(1)

    resp = httpx.get(f"{metrics_url}/metrics", timeout=5)
    after = sum_metric(resp.text, "helix_entities_total", 'instrument_id="TEST"')

    assert after >= before + 5, f"counter only moved from {before} to {after}"


# ── T1-14 ─────────────────────────────────────────────────────────────────────

def test_T1_14_prometheus_error_and_event_counters(instrument):
    import httpx
    from conftest import HERALD_URL

    metrics_url = HERALD_URL.replace(":8080", ":2112")

    def get_counter(text, name, labels):
        for line in text.splitlines():
            if line.startswith(name) and all(f'{k}="{v}"' in line for k, v in labels.items()):
                return float(line.split()[-1])
        return 0.0

    resp = httpx.get(f"{metrics_url}/metrics", timeout=5)
    errors_before = get_counter(resp.text, "helix_errors_total", {"instrument_id": "TEST"})

    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    token.add_event("helix.error", {"msg": "test"})
    token.add_event("helix.event.rfi_flagged", {"fraction": "0.5"})
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)
    time.sleep(1)

    resp = httpx.get(f"{metrics_url}/metrics", timeout=5)
    errors_after = get_counter(resp.text, "helix_errors_total", {"instrument_id": "TEST"})
    assert errors_after >= errors_before + 1, f"errors_total moved from {errors_before} to {errors_after}"


# ── T1-15 ─────────────────────────────────────────────────────────────────────

def test_T1_15_entity_timeline_query(instrument, db_cursor):
    eid = unique_id("cand")
    token = instrument.create("test-stage", id=eid).start()
    time.sleep(0.05)
    token.add_event("helix.event.rfi_flagged", {"step": "1"})
    time.sleep(0.05)
    token.add_event("helix.event.rfi_cleared", {"step": "2"})
    time.sleep(0.05)
    token.add_event("helix.event.candidate_promoted", {"step": "3"})
    token.complete()
    instrument._provider.force_flush(timeout_millis=5000)

    events = wait_for_events(db_cursor, eid, count=3)
    assert len(events) == 3
    names = [e["event_name"] for e in events]  # already ordered by timestamp_ns ASC in wait_for_events
    assert names == [
        "helix.event.rfi_flagged",
        "helix.event.rfi_cleared",
        "helix.event.candidate_promoted",
    ], f"unexpected order: {names}"
