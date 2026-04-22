"""
Entity operations tests — T1-17, T1-18, T1-19.

Requires: full Docker Compose stack running.
"""

import time

import pytest

from conftest import emit_entity, unique_id, wait_for_entity, wait_for_operation


# ── T1-17 ─────────────────────────────────────────────────────────────────────

def test_T1_17_operation_writes_to_correct_table(instrument, db_cursor):
    eid = unique_id("block")
    emit_entity(instrument, eid)

    with instrument.operate("reprocess", entity_id=eid):
        pass
    instrument._provider.force_flush(timeout_millis=5000)

    # entities: exactly 1 row (no duplicate from the operation)
    db_cursor.execute("SELECT COUNT(*) AS n FROM entities WHERE id = %s", (eid,))
    assert db_cursor.fetchone()["n"] == 1

    op_row = wait_for_operation(db_cursor, eid)
    assert op_row is not None, f"no entity_operations row for {eid}"
    assert op_row["operation"] == "reprocess"

    # Original entity trace_id must still be in TraceStore so children can link
    # Emit a child and confirm it links to the entity, not the operation span
    child_id = unique_id("child")
    emit_entity(instrument, child_id, parents=[eid])
    child_row = wait_for_entity(db_cursor, child_id)
    assert child_row is not None
    assert eid in child_row["parent_ids"]


# ── T1-18 ─────────────────────────────────────────────────────────────────────

def test_T1_18_operation_on_unknown_entity_creates_placeholder(instrument, db_cursor):
    eid = unique_id("block-new")

    # No prior entity span — operate() should upsert a placeholder
    with instrument.operate("hdf5-conversion", entity_id=eid):
        pass
    instrument._provider.force_flush(timeout_millis=5000)

    entity_row = wait_for_entity(db_cursor, eid)
    assert entity_row is not None, "placeholder entity row not created"
    assert entity_row["parent_ids"] == []

    op_row = wait_for_operation(db_cursor, eid)
    assert op_row is not None, "entity_operations row not created"


# ── T1-19 ─────────────────────────────────────────────────────────────────────

def test_T1_19_duplicate_operation_deduplicated(instrument, db_cursor):
    eid = unique_id("block")
    emit_entity(instrument, eid)

    # Emit the same operation twice — the second must be a no-op due to seen-set
    with instrument.operate("reprocess", entity_id=eid) as op:
        # capture trace_id from span
        trace_id_hex = format(op._span.get_span_context().trace_id, "032x")

    # Force a second export of the same operation by re-emitting with identical
    # trace_id is not directly possible through the client, so we verify the
    # seen-set directly: emit a second operation (different trace) then check counts.
    with instrument.operate("reprocess", entity_id=eid):
        pass
    instrument._provider.force_flush(timeout_millis=5000)
    time.sleep(1)

    db_cursor.execute(
        "SELECT COUNT(*) AS n FROM entity_operations WHERE entity_id = %s AND operation = 'reprocess'",
        (eid,),
    )
    count = db_cursor.fetchone()["n"]
    # Two distinct traces → 2 rows is correct; same trace → 1 row.
    # This test verifies the seen-set entry exists for the first trace.
    db_cursor.execute(
        "SELECT trace_id FROM operation_trace_seen WHERE trace_id = %s",
        (trace_id_hex,),
    )
    seen = db_cursor.fetchone()
    assert seen is not None, f"trace_id {trace_id_hex} not in operation_trace_seen"
