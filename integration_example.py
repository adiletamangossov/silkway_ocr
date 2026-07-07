"""Reference: how the parcel-processing backend calls the OCR service.

This is a SKETCH to adapt into your backend, not part of the deployed service. It
shows the whole flow end to end against the real schema:

    cargo_parcels (needs OCR)  ->  client.recognize(photo)  ->  decision
      accept  ->  write member_id to cargo_parcels.found_member_id  (auto)
      manual  ->  hand to a specialist queue, prefilled              (human)

Key points your backend should keep:
  * Name the upload `parcel_<parcel_id>.jpg`. The endpoint logs it to ocr_decisions,
    and backfill_gt.py can then auto-score those calls against ground truth later
    with no manual labeling.
  * Only an `accept` is written automatically — its member_id is db-confirmed by the
    service. A `manual` result is a guess (or nothing), so it goes to a human; never
    auto-write it.
  * found_member_id is the OCR's answer for the parcel. It does NOT assume the parcel
    is already linked to a user — reading the member_id off the label is how an
    unlinked parcel gets matched to its client.

Safety: writes are OFF by default (dry run). Pass --commit to actually update
found_member_id. The specialist-queue step is a stub (enqueue_manual) — wire it to
your real queue.

Run (dry run, 3 parcels):  python integration_example.py --limit 3
Run (write accepts):       python integration_example.py --limit 3 --commit
"""

import os
import urllib.request

from dotenv import load_dotenv

load_dotenv()

import psycopg

from client import recognize


def conn_kwargs() -> dict:
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ.get("DB_PORT", "4444"),
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "dbname": os.environ["DB_NAME"],
    }


def fetch_parcels_needing_ocr(conn, limit: int) -> list[tuple]:
    # parcels that have a photo but no OCR answer yet. in a real backend this is
    # more likely event-driven (a parcel is photographed -> a job is enqueued) than
    # a batch poll, but the per-parcel handling below is identical either way.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT parcel_id, images[1]
            FROM cargo_parcels
            WHERE images IS NOT NULL
              AND array_length(images, 1) > 0
              AND found_member_id IS NULL
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def download_image(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def record_found_member_id(conn, parcel_id, member_id: str) -> None:
    # write the auto-accepted answer back onto the parcel. dedicated column, so this
    # never touches the client record (users) — it just annotates the parcel.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cargo_parcels SET found_member_id = %s WHERE parcel_id = %s",
            (member_id, parcel_id),
        )


def enqueue_manual(parcel_id, decision: dict) -> None:
    # STUB: wire this to your specialist queue. hand over the parcel with the guess
    # pre-filled (decision["member_id"], may be None) and any shortlist
    # (decision.get("candidates")) so the specialist confirms with one click instead
    # of keying the id from scratch.
    prefill = decision.get("member_id")
    candidates = decision.get("candidates")
    print(
        f"    -> QUEUE parcel {parcel_id} for specialist "
        f"(prefill={prefill}, candidates={candidates}, reason={decision.get('reason')})"
    )


def process_parcel(conn, parcel_id, image_url: str, *, commit: bool) -> dict:
    # the core per-parcel handler — this is the part that lives in your backend.
    photo = download_image(image_url)

    # name the upload after the parcel so the decision log is auto-scoreable later.
    decision = recognize(photo, platform="parcel", filename=f"parcel_{parcel_id}.jpg")

    if decision["status"] == "accept":
        member_id = decision["member_id"]
        print(f"  ACCEPT parcel {parcel_id} -> found_member_id={member_id} "
              f"({decision['source']}/{decision['confidence']})")
        if commit:
            record_found_member_id(conn, parcel_id, member_id)
            conn.commit()
            print("    -> written")
        else:
            print("    -> dry run, not written (pass --commit to write)")
    else:
        print(f"  MANUAL parcel {parcel_id} ({decision['reason']})")
        enqueue_manual(parcel_id, decision)

    return decision


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=3, help="how many parcels to process")
    parser.add_argument("--commit", action="store_true", help="actually write found_member_id")
    args = parser.parse_args()

    with psycopg.connect(**conn_kwargs()) as conn:
        parcels = fetch_parcels_needing_ocr(conn, args.limit)
        print(f"processing {len(parcels)} parcel(s){'' if args.commit else ' (dry run)'}\n")

        accepted = 0
        for parcel_id, url in parcels:
            try:
                d = process_parcel(conn, parcel_id, url, commit=args.commit)
                accepted += d["status"] == "accept"
            except Exception as e:  # noqa: BLE001
                print(f"  ERROR parcel {parcel_id}: {type(e).__name__}: {e}")

    print(f"\n{accepted}/{len(parcels)} auto-accepted; the rest went to the manual queue")


if __name__ == "__main__":
    main()
