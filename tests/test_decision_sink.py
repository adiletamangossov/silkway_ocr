from decision_sink import SqliteDecisionSink, _row


def _sink(tmp_path):
    sink = SqliteDecisionSink(str(tmp_path / "ocr_decisions.db"))
    sink.ensure_schema()
    return sink


def test_log_then_read_round_trip(tmp_path):
    sink = _sink(tmp_path)
    decision = {
        "status": "accept",
        "member_id": "960662",
        "confidence": "high",
        "source": "marker",
        "reason": "marker match confirmed in db",
    }

    sink.log_decision(
        "C:/labels/image_silkway.jpeg",
        "首都波960662号",
        decision,
        platform="taobao",
        corrected_id="960662",
    )

    rows = sink.read_decisions()
    assert len(rows) == 1
    r = rows[0]
    # scalar columns are pulled out of the decision for easy querying...
    assert r["photo"] == "image_silkway.jpeg"  # stored as basename, not full path
    assert r["platform"] == "taobao"
    assert r["transcript"] == "首都波960662号"
    assert r["status"] == "accept"
    assert r["member_id"] == "960662"
    assert r["confidence"] == "high"
    assert r["source"] == "marker"
    assert r["corrected_id"] == "960662"
    # ...and the whole decision is preserved verbatim as json.
    assert r["decision"] == decision
    assert "ts" in r


def test_appends_rather_than_overwrites(tmp_path):
    sink = _sink(tmp_path)
    sink.log_decision("a.jpg", "t1", {"status": "manual"})
    sink.log_decision("b.jpg", "t2", {"status": "manual"})
    assert [r["photo"] for r in sink.read_decisions()] == ["a.jpg", "b.jpg"]


def test_candidates_stored_as_json_list(tmp_path):
    sink = _sink(tmp_path)
    decision = {
        "status": "manual",
        "member_id": None,
        "source": "db_scan",
        "candidates": ["960662", "960663"],
    }
    sink.log_decision("a.jpg", "t", decision)
    r = sink.read_decisions()[0]
    assert r["candidates"] == ["960662", "960663"]


def test_corrected_id_defaults_to_none(tmp_path):
    # at real decision time we usually don't know the verified answer yet.
    sink = _sink(tmp_path)
    sink.log_decision("a.jpg", "t", {"status": "manual", "member_id": None})
    assert sink.read_decisions()[0]["corrected_id"] is None


def test_ensure_schema_is_idempotent(tmp_path):
    sink = _sink(tmp_path)
    sink.log_decision("a.jpg", "t", {"status": "manual"})
    sink.ensure_schema()  # calling again must not drop existing rows
    assert len(sink.read_decisions()) == 1


def test_row_flattens_scalar_fields_and_basenames_photo():
    decision = {"status": "accept", "member_id": "1", "confidence": "high",
                "source": "marker", "reason": "ok"}
    r = _row("/a/b/c.jpg", "txt", decision, "pdd", "1")
    assert r["photo"] == "c.jpg"
    assert r["status"] == "accept" and r["source"] == "marker"
    assert r["decision"] == decision
    assert r["candidates"] is None  # absent in the decision -> None
