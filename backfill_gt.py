"""Backfill ground-truth member_ids into ocr_decisions and print live accuracy.

The endpoint logs every decision to the ocr_decisions table with corrected_id
NULL — at request time we don't know the verified answer. But for photos whose
filename encodes a parcel id (parcel_<id>.jpg, as build_db_eval.py names the eval
sets), the ground truth already lives in the db: the same linkage eval_platforms
trusts, cargo_parcels.parcel_id -> user_id -> users.member_id. This script fills
corrected_id from that linkage in one set-based UPDATE, then scores every
now-labeled decision and prints a per-platform accuracy table.

Decisions whose photo can't be linked (arbitrary production filenames) have no
ground truth and stay unscored — they need a human to backfill corrected_id.

Idempotent: only NULL rows are filled, so it is safe to re-run as traffic grows.

Run:  python backfill_gt.py
"""

import os

from dotenv import load_dotenv

load_dotenv()

import psycopg
from psycopg.rows import dict_row

from evaluation import aggregate, format_table, score

# how a photo filename encodes its parcel id. build_db_eval.py writes
# parcel_<parcel_id>.jpg, so the digits right after "parcel_" are the parcel id.
# postgres substring(text from pattern) returns the first parenthesized group.
PHOTO_PARCEL_RE = r"^parcel_(\d+)"


def conn_kwargs() -> dict:
    # same env vars as the rest of the pipeline; no credentials in code.
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ.get("DB_PORT", "4444"),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
    }


def backfill(conn) -> int:
    # link each still-unlabeled decision to its parcel's ground-truth member_id via
    # the parcel id embedded in the photo filename. one set-based statement over the
    # whole table; only corrected_id IS NULL rows are touched, so re-running is safe.
    # the ::text comparison sidesteps assuming parcel_id's numeric type; a photo that
    # doesn't match the pattern yields NULL and simply joins nothing.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ocr_decisions d
            SET corrected_id = u.member_id
            FROM cargo_parcels p
            JOIN users u ON u.id = p.user_id
            WHERE d.corrected_id IS NULL
              AND substring(d.photo from %s) = p.parcel_id::text
            """,
            (PHOTO_PARCEL_RE,),
        )
        return cur.rowcount


def labeled_rows(conn) -> list[dict]:
    # every decision that now has ground truth, newest linkage included. the stored
    # decision JSONB comes back as a dict, ready to hand straight to score().
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT platform, decision, corrected_id FROM ocr_decisions "
            "WHERE corrected_id IS NOT NULL ORDER BY id"
        )
        return cur.fetchall()


def total_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ocr_decisions")
        return cur.fetchone()[0]


def main():
    with psycopg.connect(**conn_kwargs()) as conn:
        filled = backfill(conn)
        conn.commit()
        rows = labeled_rows(conn)
        total = total_count(conn)

    print(f"backfilled {filled} newly-linked decision(s)")
    print(
        f"{len(rows)}/{total} decisions have ground truth "
        f"({total - len(rows)} unlinkable — no parcel id in the filename)"
    )
    if not rows:
        print("\nnothing to score yet.")
        return

    # reuse the eval scorer: correct / accepted / false_accept, per platform.
    scored = [
        {"platform": r["platform"] or "?", **score(r["corrected_id"], r["decision"])}
        for r in rows
    ]
    groups, agg = aggregate(scored)
    print()
    print(format_table(groups, agg))


if __name__ == "__main__":
    main()
