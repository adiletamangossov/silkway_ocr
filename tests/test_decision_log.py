import os

from decision_log import log_decision, read_decisions, log_path


def test_log_then_read_round_trip(tmp_path):
    path = str(tmp_path / "decisions.jsonl")
    decision = {"status": "accept", "member_id": "960662", "source": "marker"}

    log_decision(
        "C:/labels/image_silkway.jpeg",
        "首都波960662号",
        decision,
        platform="taobao",
        corrected_id="960662",
        path=path,
    )

    records = read_decisions(path)
    assert len(records) == 1
    r = records[0]
    # the photo is stored as a basename, not the full path.
    assert r["photo"] == "image_silkway.jpeg"
    assert r["platform"] == "taobao"
    assert r["transcript"] == "首都波960662号"
    assert r["decision"] == decision
    assert r["corrected_id"] == "960662"
    assert "ts" in r


def test_appends_rather_than_overwrites(tmp_path):
    path = str(tmp_path / "decisions.jsonl")
    log_decision("a.jpg", "t1", {"status": "manual"}, path=path)
    log_decision("b.jpg", "t2", {"status": "manual"}, path=path)
    records = read_decisions(path)
    assert [r["photo"] for r in records] == ["a.jpg", "b.jpg"]


def test_corrected_id_defaults_to_none(tmp_path):
    # at real decision time we usually don't know the verified answer yet.
    path = str(tmp_path / "decisions.jsonl")
    log_decision("a.jpg", "t", {"status": "manual", "member_id": None}, path=path)
    assert read_decisions(path)[0]["corrected_id"] is None


def test_read_missing_file_is_empty(tmp_path):
    assert read_decisions(str(tmp_path / "nope.jsonl")) == []


def test_log_path_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("DECISION_LOG_PATH", raising=False)
    assert log_path() == "decisions.jsonl"
    monkeypatch.setenv("DECISION_LOG_PATH", "/var/data/d.jsonl")
    assert log_path() == "/var/data/d.jsonl"
