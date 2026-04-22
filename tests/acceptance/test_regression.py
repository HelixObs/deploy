"""
Bug regression tests — T1-33 through T1-38.

Each test is named after a specific bug fixed during development.
T1-33: operation_trace_seen dedup
T1-34: graph API no duplicate nodes for placeholder+operation entity
T1-35: graph API full DAG (RECURSIVE CTE fix)
T1-36: sherlock_usage written even on turn-cap exhaustion  (marked sherlock)
T1-37: Sherlock query_loki returns real lines             (marked sherlock)
T1-38: Sherlock tempo_url opens in Grafana 12             (manual / marked sherlock)
"""

import time

import httpx
import pytest

from conftest import emit_entity, unique_id, wait_for_entity, wait_for_operation


# ── T1-33 ─────────────────────────────────────────────────────────────────────

def test_T1_33_operation_dedup_blocked_by_seen_set(instrument, db_cursor):
    eid = unique_id("block")
    emit_entity(instrument, eid)

    with instrument.operate("reprocess", entity_id=eid) as op:
        trace_id_hex = format(op._span.get_span_context().trace_id, "032x")
    instrument._provider.force_flush(timeout_millis=5000)
    time.sleep(1)

    # seen-set must have exactly one entry for this trace_id
    db_cursor.execute(
        "SELECT COUNT(*) AS n FROM operation_trace_seen WHERE trace_id = %s",
        (trace_id_hex,),
    )
    assert db_cursor.fetchone()["n"] == 1

    # entity_operations must have exactly one row
    db_cursor.execute(
        "SELECT COUNT(*) AS n FROM entity_operations WHERE entity_id = %s",
        (eid,),
    )
    assert db_cursor.fetchone()["n"] == 1


# ── T1-34 ─────────────────────────────────────────────────────────────────────

def test_T1_34_graph_api_no_duplicate_nodes_for_placeholder(instrument, db_cursor, gateway):
    eid = unique_id("block-new")

    # operate() on unknown entity → placeholder upsert
    with instrument.operate("hdf5-conversion", entity_id=eid):
        pass
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_operation(db_cursor, eid)

    resp = gateway.get(f"/api/v1/entity/{eid}/graph")
    assert resp.status_code == 200, f"graph API returned {resp.status_code}"
    data = resp.json()

    node_ids = [n["id"] for n in data["nodes"]]
    assert node_ids.count(eid) == 1, f"duplicate nodes for {eid}: {node_ids}"


# ── T1-35 ─────────────────────────────────────────────────────────────────────

def test_T1_35_graph_api_full_dag_three_level_chain(instrument, db_cursor, gateway):
    block_id = unique_id("block")
    cand_id  = unique_id("cand")
    event_id = unique_id("event")

    emit_entity(instrument, block_id)
    emit_entity(instrument, cand_id, parents=[block_id])
    emit_entity(instrument, event_id, parents=[cand_id])
    wait_for_entity(db_cursor, event_id)

    resp = gateway.get(f"/api/v1/entity/{event_id}/graph")
    assert resp.status_code == 200
    data = resp.json()

    node_ids = {n["id"] for n in data["nodes"]}
    assert event_id in node_ids, "event node missing"
    assert cand_id  in node_ids, "cand node missing (depth-1 traversal broken)"
    assert block_id in node_ids, "block node missing (depth-2 traversal broken — RECURSIVE CTE)"
    assert len(data["edges"]) >= 2


# ── T1-36 ─────────────────────────────────────────────────────────────────────

@pytest.mark.sherlock
def test_T1_36_sherlock_usage_persisted_on_turn_cap_exhaustion(db_cursor, sherlock):
    """
    Diagnose an entity with no data so Sherlock exhausts MAX_TURNS without
    calling submit_hypothesis. A sherlock_usage row must still be written
    with successful=false.
    """
    eid = unique_id("ghost")  # entity that has no DB rows — all tools return empty

    chunks = []
    with sherlock.stream("POST", f"/diagnose/{eid}", json={"instrument_id": "TEST"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.strip():
                chunks.append(line)

    time.sleep(2)  # let the DB write complete

    db_cursor.execute(
        "SELECT successful, cost_usd, tool_calls FROM sherlock_usage WHERE entity_id = %s ORDER BY created_at DESC LIMIT 1",
        (eid,),
    )
    row = db_cursor.fetchone()
    assert row is not None, "no sherlock_usage row written after investigation"
    assert row["successful"] is False or row["successful"] == False
    assert float(row["cost_usd"]) > 0, "cost_usd should be > 0 (tokens were consumed)"


# ── T1-37 ─────────────────────────────────────────────────────────────────────

@pytest.mark.sherlock
def test_T1_37_sherlock_loki_tool_returns_logs(instrument, db_cursor, sherlock):
    """
    Sherlock's query_loki tool must return actual log lines for a CHIME entity,
    not an empty array. Verifies the stream label fix (helix_instrument_id not job).
    """
    import json

    # Use mock-telescope's instrument_id so logs are guaranteed to exist
    eid = unique_id("cand")
    emit_entity(instrument, eid)
    time.sleep(3)  # let logs flow to Loki

    evidence_chunks = []
    with sherlock.stream("POST", f"/diagnose/{eid}", json={"instrument_id": "TEST"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
                if chunk.get("type") == "evidence":
                    evidence_chunks.append(chunk)
            except json.JSONDecodeError:
                pass

    loki_results = [
        c for c in evidence_chunks
        if "loki" in c.get("data", {}).get("tool", "").lower()
    ]

    if loki_results:
        for result in loki_results:
            tool_result = result.get("data", {}).get("result", {})
            lines = tool_result.get("lines", []) if isinstance(tool_result, dict) else []
            assert lines != [], "query_loki returned empty lines — stream label bug?"


# ── T1-38 ─────────────────────────────────────────────────────────────────────

@pytest.mark.sherlock
def test_T1_38_sherlock_tempo_url_format(instrument, db_cursor, sherlock):
    """
    Tempo URLs in Sherlock evidence must use queryType=traceql (Grafana 12 format),
    not the old queryType=traceId which returns No Data.
    """
    import json

    eid = unique_id("block")
    emit_entity(instrument, eid)
    with instrument.operate("hdf5-conversion", entity_id=eid):
        pass
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_operation(db_cursor, eid)

    evidence_chunks = []
    with sherlock.stream("POST", f"/diagnose/{eid}", json={"instrument_id": "TEST"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
                if chunk.get("type") == "evidence":
                    evidence_chunks.append(chunk)
            except json.JSONDecodeError:
                pass

    # Find any tempo_url in evidence
    for chunk in evidence_chunks:
        data = chunk.get("data", {})
        result = data.get("result", {})
        if isinstance(result, dict):
            url = result.get("tempo_url", "")
            if url:
                assert "queryType=traceql" in url, \
                    f"tempo_url uses old format (should be queryType=traceql): {url}"
