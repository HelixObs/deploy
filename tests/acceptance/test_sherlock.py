"""
Sherlock AI tests — T1-20 through T1-24.

All tests are marked @pytest.mark.sherlock and skipped by default.
Run with: pytest -m sherlock

Requires: ANTHROPIC_API_KEY env var and full Docker Compose stack.
"""

import json
import time

import pytest

from conftest import emit_entity, unique_id, wait_for_entity


pytestmark = pytest.mark.sherlock


def _stream_diagnosis(sherlock_client, entity_id: str, instrument_id: str = "TEST", github_token: str = "") -> list[dict]:
    """Stream a full diagnosis and return all chunks as parsed dicts."""
    chunks = []
    with sherlock_client.stream(
        "POST",
        f"/diagnose/{entity_id}",
        json={"instrument_id": instrument_id, "github_token": github_token},
    ) as resp:
        assert resp.status_code == 200, f"POST /diagnose/{entity_id} → {resp.status_code}"
        for line in resp.iter_lines():
            if line.strip():
                try:
                    chunks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return chunks


# ── T1-20 ─────────────────────────────────────────────────────────────────────

def test_T1_20_diagnosis_starts_and_streams(instrument, db_cursor, sherlock):
    eid = unique_id("cand")
    token = instrument.track("test-stage", id=eid)
    token.add_event("helix.error", {"type": "CLASSIFIER_TIMEOUT"})
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_entity(db_cursor, eid)

    chunks = _stream_diagnosis(sherlock, eid)
    assert len(chunks) > 0, "no chunks received"

    first = chunks[0]
    assert first["type"] == "step"
    assert "session_id" in first.get("data", {}), "first chunk missing session_id"

    types = {c["type"] for c in chunks}
    assert "evidence" in types or "step" in types, "no evidence or step chunks"

    done = next((c for c in chunks if c["type"] == "done"), None)
    assert done is not None, "no 'done' chunk"
    assert "model" in done.get("data", {}), "done chunk missing model"
    assert "cost_usd" in done.get("data", {}), "done chunk missing cost_usd"

    error = next((c for c in chunks if c["type"] == "error"), None)
    assert error is None, f"unexpected error chunk: {error}"


# ── T1-21 ─────────────────────────────────────────────────────────────────────

def test_T1_21_hypothesis_saved_to_memory(instrument, db_cursor, sherlock):
    eid = unique_id("cand")
    token = instrument.track("test-stage", id=eid)
    token.add_event("helix.error", {"type": "RFI_BURST", "severity": "high"})
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_entity(db_cursor, eid)

    chunks = _stream_diagnosis(sherlock, eid)
    done = next((c for c in chunks if c["type"] == "done"), None)
    assert done is not None

    if not done.get("data", {}).get("successful"):
        pytest.skip("investigation did not complete successfully (hit turn cap) — memory write skipped")

    time.sleep(2)

    db_cursor.execute(
        "SELECT classification, confidence, summary FROM instrument_memory WHERE entity_id = %s ORDER BY created_at DESC LIMIT 1",
        (eid,),
    )
    mem = db_cursor.fetchone()
    assert mem is not None, "no instrument_memory row written"
    assert mem["classification"], "classification is empty"
    assert mem["summary"], "summary is empty"

    db_cursor.execute(
        "SELECT successful, tool_calls, cost_usd FROM sherlock_usage WHERE entity_id = %s ORDER BY created_at DESC LIMIT 1",
        (eid,),
    )
    usage = db_cursor.fetchone()
    assert usage is not None, "no sherlock_usage row"
    assert usage["successful"] is True
    assert usage["tool_calls"] > 0


# ── T1-22 ─────────────────────────────────────────────────────────────────────

def test_T1_22_re_investigation_served_from_memory(instrument, db_cursor, sherlock):
    eid = unique_id("cand")
    token = instrument.track("test-stage", id=eid)
    token.add_event("helix.error", {"type": "CLASSIFIER_TIMEOUT"})
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_entity(db_cursor, eid)

    # First investigation — must succeed and write memory
    chunks1 = _stream_diagnosis(sherlock, eid)
    done1 = next((c for c in chunks1 if c["type"] == "done"), None)
    if not done1 or not done1.get("data", {}).get("successful"):
        pytest.skip("first investigation did not write memory — cannot test cache path")

    # Second investigation — must be served from memory
    chunks2 = _stream_diagnosis(sherlock, eid)
    done2 = next((c for c in chunks2 if c["type"] == "done"), None)
    assert done2 is not None

    assert done2.get("data", {}).get("model") == "memory", \
        f"second investigation should use model='memory', got: {done2.get('data', {}).get('model')}"
    assert float(done2.get("data", {}).get("cost_usd", 1)) == 0.0, \
        "memory-served response should have cost_usd=0"

    # No tool call evidence in memory-served response
    evidence = [c for c in chunks2 if c["type"] == "evidence"]
    assert len(evidence) == 0, "memory-served response should have no tool call evidence"


# ── T1-23 ─────────────────────────────────────────────────────────────────────

def test_T1_23_operator_reply_continues_investigation(instrument, db_cursor, sherlock):
    eid = unique_id("cand")
    token = instrument.track("test-stage", id=eid)
    token.add_event("helix.error", {"type": "GPU_HANG"})
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_entity(db_cursor, eid)

    # Collect first stream to get session_id
    first_chunks = []
    session_id = None
    got_question = False
    with sherlock.stream("POST", f"/diagnose/{eid}", json={"instrument_id": "TEST"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
                first_chunks.append(chunk)
                if chunk["type"] == "step" and not session_id:
                    session_id = chunk.get("data", {}).get("session_id")
                if chunk["type"] == "question":
                    got_question = True
                    break  # stop reading after first question
                if chunk["type"] == "done":
                    break
            except json.JSONDecodeError:
                pass

    if not got_question:
        pytest.skip("investigation completed without asking a question — cannot test reply path")

    assert session_id, "no session_id captured"

    # Send a reply and verify the investigation continues
    reply_chunks = []
    with sherlock.stream(
        "POST",
        f"/diagnose/{session_id}/reply",
        json={"content": "Yes, the GPU temperature was above 85°C."},
    ) as resp:
        assert resp.status_code == 200, f"reply returned {resp.status_code}"
        for line in resp.iter_lines():
            if line.strip():
                try:
                    reply_chunks.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    assert len(reply_chunks) > 0, "no chunks from reply stream"
    reply_types = {c["type"] for c in reply_chunks}
    assert reply_types & {"step", "evidence", "hypothesis", "done"}, \
        f"reply produced no meaningful chunks: {reply_types}"


# ── T1-24 ─────────────────────────────────────────────────────────────────────

def test_T1_24_memory_crud(instrument, db_cursor, sherlock):
    # Seed at least one memory entry by running an investigation
    eid = unique_id("cand")
    token = instrument.track("test-stage", id=eid)
    token.add_event("helix.error", {"type": "CLASSIFIER_TIMEOUT"})
    instrument.complete(token)
    instrument._provider.force_flush(timeout_millis=5000)
    wait_for_entity(db_cursor, eid)

    _stream_diagnosis(sherlock, eid)
    time.sleep(2)

    # GET /memory/TEST
    resp = sherlock.get("/memory/TEST")
    assert resp.status_code == 200
    entries = resp.json()
    assert isinstance(entries, list)

    if not entries:
        pytest.skip("no memory entries after investigation — cannot test DELETE")

    # Find the entry we just created
    our_entry = next((e for e in entries if e.get("entity_id") == eid), None)
    if not our_entry:
        pytest.skip("our memory entry not found — investigation may not have succeeded")

    entry_id = our_entry["id"]

    # DELETE
    del_resp = sherlock.delete(f"/memory/{entry_id}")
    assert del_resp.status_code == 204

    # GET again — entry must be gone
    resp2 = sherlock.get("/memory/TEST")
    assert resp2.status_code == 200
    remaining_ids = [e["id"] for e in resp2.json()]
    assert entry_id not in remaining_ids, f"deleted entry {entry_id} still in memory list"
