import os
import sqlite3

from extraction import (
    extract_user_id,
    find_id_candidates,
    find_member_id_candidates,
    has_warehouse_marker,
    wildcard_patterns,
)

# the validation layer answers one question: does this user id exist in
# silkway's client database? a clean OCR parse is trusted only once the id is
# confirmed here, because the VLM can hallucinate a digit and still produce a
# well-formed number.

# the rest of the pipeline talks to a store through exists(). to use the real
# database later, write another store whose exists() hits postgres / mysql / an
# http api and pass it in instead. nothing else changes.


class UserIDStore:
    def exists(self, user_id: str) -> bool:
        raise NotImplementedError

    def find_matching(self, like_patterns: list[str]) -> list[str]:
        # return the real member ids matching any of the given SQL LIKE patterns
        # (where `_` is one unknown char). used to recover an id with a single
        # obscured/misread digit. empty input -> empty result.
        raise NotImplementedError


class SqliteUserIDStore(UserIDStore):
    # local stand-in database so the pipeline is testable before the real one is
    # wired. the file path comes from an env var, never hardcoded.
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("USER_ID_DB_PATH", "user_ids.db")
        self._ensure_schema()

    def _ensure_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS clients (user_id TEXT PRIMARY KEY)"
            )

    def add(self, user_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO clients (user_id) VALUES (?)", (user_id,)
            )

    def exists(self, user_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT 1 FROM clients WHERE user_id = ? LIMIT 1", (user_id,)
            )
            return cur.fetchone() is not None

    def find_matching(self, like_patterns: list[str]) -> list[str]:
        if not like_patterns:
            return []
        # one OR-ed query. only the fixed "user_id LIKE ?" fragment is repeated
        # into the sql; every pattern value is still bound, never interpolated.
        clause = " OR ".join(["user_id LIKE ?"] * len(like_patterns))
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"SELECT user_id FROM clients WHERE {clause}", like_patterns
            )
            return [row[0] for row in cur.fetchall()]


class PostgresUserIDStore(UserIDStore):
    # the real client database. member ids live in users.member_id as text, so
    # the OCR string is compared directly with no cast. connections come from the
    # shared pool (db.connection); no credentials are ever hardcoded.
    def exists(self, user_id: str) -> bool:
        # imported lazily so this module loads even where the pool / psycopg isn't
        # present (e.g. the offline test run that only uses the sqlite stub).
        from db import connection

        # parameterized query: the value is bound by the driver, never string-
        # formatted into the sql, so a malicious transcript can't inject.
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM users WHERE member_id = %s LIMIT 1", (user_id,)
                )
                return cur.fetchone() is not None

    def find_matching(self, like_patterns: list[str]) -> list[str]:
        if not like_patterns:
            return []
        from db import connection

        # LIKE ANY(array) matches member_id against every pattern in one round
        # trip; psycopg binds the list as a postgres text array, no string-building.
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT member_id FROM users WHERE member_id LIKE ANY(%s)",
                    (like_patterns,),
                )
                return [row[0] for row in cur.fetchall()]


def validate(user_id: str | None, store: UserIDStore) -> dict:
    # decide what happens to one parsed id:
    #   parsed and in db     -> auto-accept
    #   parsed but not in db -> manual queue, prefilled with the guess
    #   nothing parsed       -> manual queue, nothing to prefill
    if user_id is None:
        return {"status": "manual", "user_id": None, "reason": "no id parsed"}
    if store.exists(user_id):
        return {"status": "accept", "user_id": user_id, "reason": "found in db"}
    return {"status": "manual", "user_id": user_id, "reason": "not in db"}


def resolve_member_id(transcript: str, store: UserIDStore) -> dict:
    # full decision over a raw transcript, marker-first with a db-assisted
    # fallback for when the warehouse marker is missing or misread.
    #
    # returned dict:
    #   status     accept | manual
    #   member_id  the id to use / pre-fill, or None
    #   confidence high (marker + db) | low (single db-valid guess) | none
    #   source     marker | db_scan | db_wildcard | None
    #   reason     human-readable explanation
    #   candidates only present when a fallback is ambiguous (>1 db hit)

    # primary path: the id anchored on the warehouse marker.
    marker_id = extract_user_id(transcript)
    if marker_id is not None:
        if store.exists(marker_id):
            return {
                "status": "accept",
                "member_id": marker_id,
                "confidence": "high",
                "source": "marker",
                "reason": "marker match confirmed in db",
            }
        # the marker located a specific id but it is not in the db. that points
        # at an OCR digit error in the right field; other 号-numbers on the label
        # are decoys, so do not run the fallback. send it to manual with the
        # marker reading pre-filled (digit-swap near-miss handling is separate).
        return {
            "status": "manual",
            "member_id": marker_id,
            "confidence": "low",
            "source": "marker",
            "reason": "marker parsed but id not in db",
        }

    # fuzzy positional-marker path: OCR frequently mangles the exact 首都波 glyphs
    # (首都表 / 首部城) or spaces them out, so the exact-marker path above misses even
    # on a genuine warehouse label. when the label still reads as the warehouse's
    # (a fuzzy marker is present) AND exactly one 5-7 digit run terminating in 号 is
    # a real client, trust it as the member_id. two guards bound the risk: the
    # marker requirement rejects non-warehouse labels, and the single-valid-run
    # requirement rejects ambiguous ones. residual risk: a clean single-digit
    # misread that itself lands on a real client can auto-accept — that is the
    # price of trusting a mangled marker, unlike the exact path where a perfectly
    # read marker correlates with cleanly read digits.
    if has_warehouse_marker(transcript):
        id_runs = find_id_candidates(transcript)
        valid_runs = [c for c in id_runs if store.exists(c)]
        if len(valid_runs) == 1:
            return {
                "status": "accept",
                "member_id": valid_runs[0],
                "confidence": "high",
                "source": "marker_fuzzy",
                "reason": "warehouse marker present; unique db-valid id before 号",
            }

    # fallback path: marker missing or ambiguous. real labels often don't follow
    # the 首都波...号 layout at all, so cast the widest net: every 5-7 digit run on
    # the label, kept only if it is a real member id in the db. the db is what
    # tells a true id apart from same-length decoys (a 5-digit hotline, short code).
    candidates = find_member_id_candidates(transcript)
    valid = [c for c in candidates if store.exists(c)]

    if len(valid) == 1:
        # one db-valid candidate. strong hint, but without the marker we are not
        # certain it is the member_id field, so pre-fill manual rather than
        # auto-accept.
        return {
            "status": "manual",
            "member_id": valid[0],
            "confidence": "low",
            "source": "db_scan",
            "reason": "marker not found; one db-valid digit run",
        }
    if len(valid) > 1:
        # several db-valid candidates: ambiguous. surface them all for the human.
        return {
            "status": "manual",
            "member_id": None,
            "confidence": "none",
            "source": "db_scan",
            "reason": "marker not found; multiple db-valid digit runs",
            "candidates": valid,
        }

    # no exact db hit. the id may be one digit off — obscured by a sorting-pen
    # scrawl (a digit dropped from the run) or simply misread (e.g. 6 read as 8).
    # ask the db which real id each run could have been via single-blank LIKE
    # patterns. this is the fuzziest path, so it never auto-accepts.
    return _resolve_by_wildcard(candidates, store)


# how many fuzzy hits we are willing to hand a human. one obscured digit in a
# dense id range (see hided.jpg: 966_38 matched ~all of 966038..966938) can hit
# dozens of real clients. a short shortlist is worth surfacing; a long one is
# noise, so we treat it as unrecoverable and send a plain manual ticket instead.
WILDCARD_MAX_CANDIDATES = 3


def _resolve_by_wildcard(candidates: list[str], store: UserIDStore) -> dict:
    # collect the single-unknown LIKE patterns for every scanned run, ask the db
    # for real ids matching any of them, and decide on how many came back.
    patterns: list[str] = []
    for run in candidates:
        for p in wildcard_patterns(run):
            if p not in patterns:
                patterns.append(p)

    hits: list[str] = []
    for member_id in store.find_matching(patterns):
        # a `_` matches any char; keep only all-digit ids so a wildcard can't
        # land on a soft-deleted "<id>_deleted_..." row or other noise.
        if member_id.isdigit() and member_id not in hits:
            hits.append(member_id)

    if len(hits) == 1:
        # exactly one real id is one digit away from what we read. strong enough
        # to pre-fill the manual queue, but it is an inference, so not auto-accept.
        return {
            "status": "manual",
            "member_id": hits[0],
            "confidence": "low",
            "source": "db_wildcard",
            "reason": "no exact match; one db id within a single obscured/misread digit",
        }
    if 1 < len(hits) <= WILDCARD_MAX_CANDIDATES:
        # a short shortlist a human can pick from.
        return {
            "status": "manual",
            "member_id": None,
            "confidence": "none",
            "source": "db_wildcard",
            "reason": "no exact match; a few db ids within a single digit",
            "candidates": hits,
        }
    if len(hits) > WILDCARD_MAX_CANDIDATES:
        # the obscured digit lands in a dense id range; too many real clients fit
        # to guess. it is genuinely unrecoverable from the image, so route to a
        # plain manual ticket rather than dumping a long ambiguous list.
        return {
            "status": "manual",
            "member_id": None,
            "confidence": "none",
            "source": None,
            "reason": f"no exact match; {len(hits)} db ids within a single digit, unrecoverable",
        }
    return {
        "status": "manual",
        "member_id": None,
        "confidence": "none",
        "source": None,
        "reason": "marker not found; no db-valid digit run, exact or within one digit",
    }
