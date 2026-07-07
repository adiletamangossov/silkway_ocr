from manual_queue import SqliteManualQueue, _queue_row


def _queue(tmp_path):
    q = SqliteManualQueue(str(tmp_path / "manual_queue.db"))
    q.ensure_schema()
    return q


def test_enqueue_then_list_pending(tmp_path):
    q = _queue(tmp_path)
    decision = {
        "status": "manual",
        "member_id": "913783",
        "confidence": "low",
        "source": "db_scan",
        "reason": "marker not found; one db-valid digit run",
    }
    qid = q.enqueue(2671376, "parcel_2671376.jpg", decision, transcript="...首都波...")

    pending = q.list_pending()
    assert len(pending) == 1
    r = pending[0]
    assert r["id"] == qid
    assert r["parcel_id"] == "2671376"  # stored as text, generic across id types
    assert r["prefill_member_id"] == "913783"
    assert r["source"] == "db_scan"
    assert r["status"] == "pending"
    assert r["transcript"] == "...首都波..."


def test_candidates_round_trip_as_list(tmp_path):
    q = _queue(tmp_path)
    decision = {"status": "manual", "member_id": None, "candidates": ["960662", "960663"]}
    q.enqueue(1, "p.jpg", decision)
    assert q.list_pending()[0]["candidates"] == ["960662", "960663"]


def test_resolve_removes_from_pending_and_records_answer(tmp_path):
    q = _queue(tmp_path)
    qid = q.enqueue(42, "p.jpg", {"status": "manual", "member_id": None})

    row = q.resolve(qid, "968690", resolved_by="specialist-1")
    assert row is not None
    assert row["status"] == "resolved"
    assert row["resolved_member_id"] == "968690"
    assert row["resolved_by"] == "specialist-1"
    assert row["parcel_id"] == "42"
    # once resolved it leaves the pending work list.
    assert q.list_pending() == []


def test_double_resolve_is_a_noop(tmp_path):
    q = _queue(tmp_path)
    qid = q.enqueue(1, "p.jpg", {"status": "manual", "member_id": None})
    assert q.resolve(qid, "111111") is not None
    # a second resolve must not overwrite the first specialist's answer.
    assert q.resolve(qid, "222222") is None


def test_resolve_unknown_id_returns_none(tmp_path):
    q = _queue(tmp_path)
    assert q.resolve(999, "111111") is None


def test_queue_row_flattens_decision():
    d = {"status": "manual", "member_id": "1", "confidence": "low",
         "source": "db_scan", "reason": "x", "candidates": ["1", "2"]}
    r = _queue_row(7, "p.jpg", d, "txt")
    assert r["parcel_id"] == "7" and r["prefill_member_id"] == "1"
    assert r["candidates"] == ["1", "2"] and r["transcript"] == "txt"
