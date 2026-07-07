"""FastAPI backend that turns a parcel photo into an action.

This is the parcel-processing service (distinct from the Modal OCR endpoint it
calls). A specialist uploads a photo for a parcel; the service asks the OCR
endpoint to read the member_id, then:

    accept  ->  write the db-confirmed member_id to cargo_parcels.found_member_id
    manual  ->  drop the parcel into the ocr_manual_queue table for a specialist,
                with the OCR guess pre-filled

and exposes the queue so a specialist UI can list pending items and resolve them
(which also writes found_member_id, closing the loop).

The queue, the OCR recognizer, and the found_member_id writer are injected as
dependencies, so the routing is unit-testable with stubs (see tests) without
touching Modal or the production db.

Run:  uvicorn backend_app:app --reload
"""

import os

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse

# env for the db writer + the OCR client (endpoint url / token). loaded at import
# so `uvicorn backend_app:app` picks it up; harmless when imported by tests.
load_dotenv()

from client import recognize
from manual_queue import PostgresManualQueue

app = FastAPI(
    title="SilkWay parcel backend",
    summary="Read a parcel's member_id via the OCR service; auto-accept or queue.",
)

# the specialist review page, served as-is at GET /. read once at import; it is a
# static shell that fetches queue data from the api with the specialist's token.
_UI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "specialist_ui.html")
with open(_UI_PATH, encoding="utf-8") as _f:
    _UI_HTML = _f.read()


# --- auth -----------------------------------------------------------------------


def require_auth(authorization: str | None = Header(default=None)):
    # gate the specialist-facing routes with a bearer token from BACKEND_API_TOKEN.
    # enforced only when the token is configured, so local dev / tests run open; set
    # it in every real deployment. this is the internal backend's own token, separate
    # from the OCR endpoint's API_TOKEN. /health stays open (no dependency).
    expected = os.environ.get("BACKEND_API_TOKEN")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


# --- dependencies (overridden in tests) -----------------------------------------

_queue = None


def get_queue() -> PostgresManualQueue:
    # lazily build + schema-init one shared queue. tests override this dependency
    # with a SqliteManualQueue, so the Postgres one is never constructed there.
    global _queue
    if _queue is None:
        q = PostgresManualQueue()
        q.ensure_schema()
        _queue = q
    return _queue


def get_recognizer():
    # the OCR call. returns the resolver decision dict (incl. transcript). named
    # per parcel so the endpoint's decision log stays auto-scoreable by backfill_gt.
    def _recognize(image_bytes: bytes, parcel_id) -> dict:
        return recognize(image_bytes, platform="parcel", filename=f"parcel_{parcel_id}.jpg")

    return _recognize


def get_parcel_writer():
    # writes the accepted / resolved member_id onto the parcel. dedicated column,
    # so the client record (users) is never touched.
    import psycopg

    def _write(parcel_id, member_id: str) -> None:
        conn_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": os.environ.get("DB_PORT", "4444"),
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "dbname": os.environ["DB_NAME"],
        }
        with psycopg.connect(**conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cargo_parcels SET found_member_id = %s WHERE parcel_id = %s",
                    (member_id, parcel_id),
                )
            conn.commit()

    return _write


# --- routes ---------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def specialist_ui():
    # open: this is just the app shell (html/js). the data it loads is behind the
    # bearer token, which the specialist enters in the page.
    return _UI_HTML


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parcels/{parcel_id}/recognize", dependencies=[Depends(require_auth)])
async def recognize_parcel(
    parcel_id: int,
    file: UploadFile = File(...),
    image_url: str | None = Form(default=None),
    queue=Depends(get_queue),
    recognizer=Depends(get_recognizer),
    write_parcel=Depends(get_parcel_writer),
):
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="empty image file")

    decision = recognizer(image_bytes, parcel_id)

    if decision.get("status") == "accept":
        # db-confirmed by the OCR service — safe to write automatically.
        write_parcel(parcel_id, decision["member_id"])
        return {"action": "accepted", "parcel_id": parcel_id,
                "member_id": decision["member_id"], "decision": decision}

    # a guess (or nothing): a human decides. queue it with the guess pre-filled.
    # image_url (optional) is stored so the review UI can show the actual photo.
    queue_id = queue.enqueue(
        parcel_id, file.filename, decision,
        transcript=decision.get("transcript"), image_url=image_url,
    )
    return {"action": "queued", "parcel_id": parcel_id, "queue_id": queue_id,
            "decision": decision}


@app.get("/manual-queue", dependencies=[Depends(require_auth)])
def manual_queue(queue=Depends(get_queue)):
    # the specialist work list: pending items with the OCR guess + candidates.
    return {"pending": queue.list_pending()}


@app.post("/manual-queue/{item_id}/resolve", dependencies=[Depends(require_auth)])
def resolve_item(
    item_id: int,
    member_id: str = Form(...),
    resolved_by: str | None = Form(default=None),
    queue=Depends(get_queue),
    write_parcel=Depends(get_parcel_writer),
):
    row = queue.resolve(item_id, member_id, resolved_by)
    if row is None:
        raise HTTPException(status_code=404, detail="queue item not found or already resolved")

    # the specialist's verified id is now the parcel's answer.
    write_parcel(int(row["parcel_id"]), member_id)
    return {"resolved": row}
