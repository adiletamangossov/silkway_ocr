import io

import pytest
from fastapi.testclient import TestClient

import backend_app
from manual_queue import SqliteManualQueue


@pytest.fixture
def client(tmp_path, monkeypatch):
    # default the auth OFF for routing tests, regardless of any BACKEND_API_TOKEN a
    # local .env set (backend_app.load_dotenv runs at import). the auth tests below
    # opt back in with monkeypatch.setenv, which runs after this fixture.
    monkeypatch.delenv("BACKEND_API_TOKEN", raising=False)

    # a real SQLite-backed queue, plus stubbed OCR + parcel-writer so the routing is
    # exercised end to end without Modal or the production db. the recognizer's
    # verdict is driven by the uploaded bytes, so a test picks accept vs manual.
    queue = SqliteManualQueue(str(tmp_path / "q.db"))
    queue.ensure_schema()

    writes: list[tuple] = []

    def fake_recognizer():
        def _rec(image_bytes, parcel_id):
            if image_bytes == b"ACCEPT":
                return {"status": "accept", "member_id": "960662",
                        "confidence": "high", "source": "marker", "transcript": "t"}
            return {"status": "manual", "member_id": "913783", "confidence": "low",
                    "source": "db_scan", "reason": "one db-valid run", "transcript": "t"}

        return _rec

    def fake_writer():
        def _write(parcel_id, member_id):
            writes.append((parcel_id, member_id))

        return _write

    backend_app.app.dependency_overrides[backend_app.get_queue] = lambda: queue
    backend_app.app.dependency_overrides[backend_app.get_recognizer] = fake_recognizer
    backend_app.app.dependency_overrides[backend_app.get_parcel_writer] = fake_writer

    c = TestClient(backend_app.app)
    c.writes = writes  # expose for assertions
    c.queue = queue
    yield c
    backend_app.app.dependency_overrides.clear()


def _upload(content: bytes):
    return {"file": ("parcel.jpg", io.BytesIO(content), "image/jpeg")}


def test_accept_writes_found_member_id_and_does_not_queue(client):
    resp = client.post("/parcels/555/recognize", files=_upload(b"ACCEPT"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "accepted"
    assert body["member_id"] == "960662"
    # the parcel got the id written, and nothing landed in the manual queue.
    assert client.writes == [(555, "960662")]
    assert client.queue.list_pending() == []


def test_manual_enqueues_with_prefill_and_no_write(client):
    resp = client.post("/parcels/777/recognize", files=_upload(b"whatever"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "queued"
    assert body["queue_id"] > 0
    # not auto-written; instead a pending item carries the guess for a specialist.
    assert client.writes == []
    pending = client.get("/manual-queue").json()["pending"]
    assert len(pending) == 1
    assert pending[0]["parcel_id"] == "777"
    assert pending[0]["prefill_member_id"] == "913783"


def test_resolve_closes_the_loop(client):
    client.post("/parcels/777/recognize", files=_upload(b"manual"))
    item_id = client.get("/manual-queue").json()["pending"][0]["id"]

    resp = client.post(
        f"/manual-queue/{item_id}/resolve",
        data={"member_id": "913783", "resolved_by": "spec-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["resolved"]["resolved_member_id"] == "913783"
    # resolving writes the verified id onto the parcel and clears the queue.
    assert client.writes == [(777, "913783")]
    assert client.get("/manual-queue").json()["pending"] == []


def test_resolve_unknown_item_404s(client):
    resp = client.post("/manual-queue/999/resolve", data={"member_id": "1"})
    assert resp.status_code == 404


def test_empty_upload_400s(client):
    resp = client.post("/parcels/1/recognize", files=_upload(b""))
    assert resp.status_code == 400


def test_auth_enforced_when_token_configured(client, monkeypatch):
    monkeypatch.setenv("BACKEND_API_TOKEN", "s3cret")

    # no token -> 401
    assert client.post("/parcels/1/recognize", files=_upload(b"ACCEPT")).status_code == 401
    # wrong token -> 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/parcels/1/recognize", files=_upload(b"ACCEPT"), headers=bad).status_code == 401
    # right token -> 200
    ok = {"Authorization": "Bearer s3cret"}
    assert client.post("/parcels/1/recognize", files=_upload(b"ACCEPT"), headers=ok).status_code == 200
    # the queue list is gated too
    assert client.get("/manual-queue").status_code == 401
    assert client.get("/manual-queue", headers=ok).status_code == 200


def test_health_stays_open_with_token_configured(client, monkeypatch):
    monkeypatch.setenv("BACKEND_API_TOKEN", "s3cret")
    assert client.get("/health").status_code == 200
