import json
import os

# durable sink for the resolution decisions the deployed endpoint makes. the
# file-based decision_log.py is fine for local entrypoints, but a Modal container
# has an ephemeral filesystem, so the endpoint writes each decision to a database
# instead. one row per request accumulates the labeled dataset any future accuracy
# audit or fine-tuning run needs, right next to the ground truth (the parcel ->
# users.member_id linkage) so the two can be joined later.
#
# same shape as validation.py's stores: an abstract interface, a Postgres
# implementation for production, and a SQLite stand-in so the logic is testable
# offline without a live database.

# the columns we pull out of the decision dict for easy querying. the full decision
# is also stored verbatim as json, so nothing is lost even if the shape grows.
_SCALAR_FIELDS = ("status", "member_id", "confidence", "source", "reason")


def _row(photo, transcript, decision, platform, corrected_id):
    # flatten one decision into the values a sink row stores. pure, so both the
    # sqlite and postgres implementations share it and it can be unit-tested.
    return {
        "photo": os.path.basename(str(photo)) if photo else None,
        "platform": platform,
        "transcript": transcript,
        "status": decision.get("status"),
        "member_id": decision.get("member_id"),
        "confidence": decision.get("confidence"),
        "source": decision.get("source"),
        "reason": decision.get("reason"),
        "candidates": decision.get("candidates"),
        "decision": decision,
        "corrected_id": corrected_id,
    }


class DecisionSink:
    def ensure_schema(self) -> None:
        # create the decisions table if it does not exist. additive and idempotent;
        # safe to call on every container start.
        raise NotImplementedError

    def log_decision(
        self,
        photo,
        transcript: str,
        decision: dict,
        platform: str | None = None,
        corrected_id: str | None = None,
    ) -> None:
        # append one decision. corrected_id is the human-verified answer: usually
        # unknown at decision time (None) and backfilled later from the manual queue.
        raise NotImplementedError

    def read_decisions(self) -> list[dict]:
        # load every logged row back, oldest first. used to audit accuracy and to
        # verify the sink is recording; mirrors decision_log.read_decisions.
        raise NotImplementedError


class SqliteDecisionSink(DecisionSink):
    # offline stand-in so the sink is testable without a live database. json-typed
    # columns are stored as TEXT (json string), matching what read_decisions parses.
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get(
            "DECISION_DB_PATH", "ocr_decisions.db"
        )

    def ensure_schema(self) -> None:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ocr_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL DEFAULT (datetime('now')),
                    photo TEXT,
                    platform TEXT,
                    transcript TEXT,
                    status TEXT,
                    member_id TEXT,
                    confidence TEXT,
                    source TEXT,
                    reason TEXT,
                    candidates TEXT,
                    decision TEXT NOT NULL,
                    corrected_id TEXT
                )
                """
            )

    def log_decision(self, photo, transcript, decision, platform=None, corrected_id=None):
        import sqlite3

        r = _row(photo, transcript, decision, platform, corrected_id)
        candidates = json.dumps(r["candidates"], ensure_ascii=False) if r["candidates"] else None
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO ocr_decisions
                    (photo, platform, transcript, status, member_id, confidence,
                     source, reason, candidates, decision, corrected_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["photo"], r["platform"], r["transcript"], r["status"],
                    r["member_id"], r["confidence"], r["source"], r["reason"],
                    candidates, json.dumps(r["decision"], ensure_ascii=False),
                    r["corrected_id"],
                ),
            )

    def read_decisions(self) -> list[dict]:
        import sqlite3

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM ocr_decisions ORDER BY id")
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                d["decision"] = json.loads(d["decision"]) if d["decision"] else None
                d["candidates"] = json.loads(d["candidates"]) if d["candidates"] else None
                rows.append(d)
            return rows


class PostgresDecisionSink(DecisionSink):
    # production sink: writes to an ocr_decisions table in the same database the
    # validator already reads. the table is dedicated to this app and only ever
    # created/inserted into — users and cargo_parcels are never touched. connections
    # come from the shared pool (db.connection); no credentials in code.
    def ensure_schema(self) -> None:
        from db import connection

        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ocr_decisions (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                        photo TEXT,
                        platform TEXT,
                        transcript TEXT,
                        status TEXT,
                        member_id TEXT,
                        confidence TEXT,
                        source TEXT,
                        reason TEXT,
                        candidates JSONB,
                        decision JSONB NOT NULL,
                        corrected_id TEXT
                    )
                    """
                )

    def log_decision(self, photo, transcript, decision, platform=None, corrected_id=None):
        from psycopg.types.json import Jsonb

        from db import connection

        r = _row(photo, transcript, decision, platform, corrected_id)
        candidates = Jsonb(r["candidates"]) if r["candidates"] is not None else None
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ocr_decisions
                        (photo, platform, transcript, status, member_id, confidence,
                         source, reason, candidates, decision, corrected_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        r["photo"], r["platform"], r["transcript"], r["status"],
                        r["member_id"], r["confidence"], r["source"], r["reason"],
                        candidates, Jsonb(r["decision"]), r["corrected_id"],
                    ),
                )

    def read_decisions(self) -> list[dict]:
        from psycopg.rows import dict_row

        from db import connection

        with connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM ocr_decisions ORDER BY id")
                return cur.fetchall()
