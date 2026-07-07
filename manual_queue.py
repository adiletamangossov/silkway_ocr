import json
import os

# the manual review queue: parcels the OCR service could not auto-accept land here
# as a row, and a specialist resolves each one. a database table (not an in-memory
# list) so the queue survives restarts and many workers share it. same shape as
# validation.py / decision_sink.py: an abstract interface, a Postgres implementation
# for production, and a SQLite stand-in so the logic is testable offline.
#
# lifecycle of a row:
#   enqueue  -> status 'pending', carries the OCR guess (prefill) + candidates
#   resolve  -> status 'resolved', records the specialist's verified member_id


def _queue_row(parcel_id, photo, decision: dict, transcript: str | None, image_url=None) -> dict:
    # flatten one manual decision into the columns a queue row stores. pure, so both
    # implementations share it and it is unit-testable. image_url (when the caller
    # has one, e.g. the parcel's cargo_parcels.images[1]) lets the specialist UI show
    # the actual photo next to the guess.
    return {
        "parcel_id": str(parcel_id),
        "photo": photo,
        "image_url": image_url,
        "prefill_member_id": decision.get("member_id"),
        "candidates": decision.get("candidates"),
        "confidence": decision.get("confidence"),
        "source": decision.get("source"),
        "reason": decision.get("reason"),
        "transcript": transcript,
    }


class ManualQueue:
    def ensure_schema(self) -> None:
        raise NotImplementedError

    def enqueue(self, parcel_id, photo, decision: dict, transcript: str | None = None,
                image_url: str | None = None) -> int:
        # add a pending item; return its queue id.
        raise NotImplementedError

    def list_pending(self) -> list[dict]:
        # every unresolved item, oldest first (a FIFO work list for specialists).
        raise NotImplementedError

    def resolve(self, item_id: int, member_id: str, resolved_by: str | None = None) -> dict | None:
        # mark an item resolved with the specialist's verified id. returns the
        # updated row (so the caller can write found_member_id onto the parcel), or
        # None if the id is unknown / already resolved.
        raise NotImplementedError


class SqliteManualQueue(ManualQueue):
    # offline stand-in so the queue is testable without a live database.
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("MANUAL_QUEUE_DB_PATH", "manual_queue.db")

    def ensure_schema(self) -> None:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ocr_manual_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    parcel_id TEXT NOT NULL,
                    photo TEXT,
                    image_url TEXT,
                    prefill_member_id TEXT,
                    candidates TEXT,
                    confidence TEXT,
                    source TEXT,
                    reason TEXT,
                    transcript TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolved_member_id TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT
                )
                """
            )

    def enqueue(self, parcel_id, photo, decision, transcript=None, image_url=None) -> int:
        import sqlite3

        r = _queue_row(parcel_id, photo, decision, transcript, image_url)
        candidates = json.dumps(r["candidates"], ensure_ascii=False) if r["candidates"] else None
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO ocr_manual_queue
                    (parcel_id, photo, image_url, prefill_member_id, candidates,
                     confidence, source, reason, transcript)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["parcel_id"], r["photo"], r["image_url"], r["prefill_member_id"],
                    candidates, r["confidence"], r["source"], r["reason"], r["transcript"],
                ),
            )
            return cur.lastrowid

    def list_pending(self) -> list[dict]:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM ocr_manual_queue WHERE status = 'pending' ORDER BY id"
            )
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                d["candidates"] = json.loads(d["candidates"]) if d["candidates"] else None
                rows.append(d)
            return rows

    def resolve(self, item_id, member_id, resolved_by=None) -> dict | None:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # only a still-pending row flips, so a double-resolve is a no-op (returns
            # None) rather than silently overwriting the first specialist's answer.
            cur = conn.execute(
                """
                UPDATE ocr_manual_queue
                SET status = 'resolved', resolved_member_id = ?, resolved_by = ?,
                    resolved_at = datetime('now')
                WHERE id = ? AND status = 'pending'
                """,
                (member_id, resolved_by, item_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM ocr_manual_queue WHERE id = ?", (item_id,)
            ).fetchone()
            d = dict(row)
            d["candidates"] = json.loads(d["candidates"]) if d["candidates"] else None
            return d


class PostgresManualQueue(ManualQueue):
    # production queue: a dedicated ocr_manual_queue table in the same database.
    # additive (CREATE TABLE IF NOT EXISTS); users and cargo_parcels are untouched.
    def __init__(self):
        self.conn_kwargs = {
            "host": os.environ["DB_HOST"],
            "port": os.environ.get("DB_PORT", "4444"),
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "dbname": os.environ["DB_NAME"],
        }

    def ensure_schema(self) -> None:
        import psycopg

        with psycopg.connect(**self.conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ocr_manual_queue (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        parcel_id TEXT NOT NULL,
                        photo TEXT,
                        image_url TEXT,
                        prefill_member_id TEXT,
                        candidates JSONB,
                        confidence TEXT,
                        source TEXT,
                        reason TEXT,
                        transcript TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        resolved_member_id TEXT,
                        resolved_by TEXT,
                        resolved_at TIMESTAMPTZ
                    )
                    """
                )
                # upgrade a table created before image_url existed (the live table was
                # already created once). additive and idempotent.
                cur.execute(
                    "ALTER TABLE ocr_manual_queue ADD COLUMN IF NOT EXISTS image_url TEXT"
                )

    def enqueue(self, parcel_id, photo, decision, transcript=None, image_url=None) -> int:
        import psycopg
        from psycopg.types.json import Jsonb

        r = _queue_row(parcel_id, photo, decision, transcript, image_url)
        candidates = Jsonb(r["candidates"]) if r["candidates"] is not None else None
        with psycopg.connect(**self.conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ocr_manual_queue
                        (parcel_id, photo, image_url, prefill_member_id, candidates,
                         confidence, source, reason, transcript)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        r["parcel_id"], r["photo"], r["image_url"], r["prefill_member_id"],
                        candidates, r["confidence"], r["source"], r["reason"], r["transcript"],
                    ),
                )
                return cur.fetchone()[0]

    def list_pending(self) -> list[dict]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(**self.conn_kwargs) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM ocr_manual_queue WHERE status = 'pending' ORDER BY id"
                )
                return cur.fetchall()

    def resolve(self, item_id, member_id, resolved_by=None) -> dict | None:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(**self.conn_kwargs) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE ocr_manual_queue
                    SET status = 'resolved', resolved_member_id = %s, resolved_by = %s,
                        resolved_at = now()
                    WHERE id = %s AND status = 'pending'
                    RETURNING *
                    """,
                    (member_id, resolved_by, item_id),
                )
                return cur.fetchone()
