"""Build a real-photo eval set straight from the production DB.

Pulls recent cargo_parcels that have a photo AND a resolvable client member_id
(via user_id -> users.member_id), downloads the photos locally, and writes a
manifest that modal_app.py::eval_platforms can consume. Ground truth is the
client's member_id, so we can score the pipeline against real labels.

Photos are real client parcels (PII): they land in eval_samples/db/, which is
git-ignored by the *.jpg / *.jpeg rules.

Run:  python build_db_eval.py [N]
"""

import json
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv()

import psycopg

# usage: python build_db_eval.py [N] [recent|random]
#   recent (default) -> the N most recent parcels (current framing practice)
#   random           -> a uniform sample across the whole table, i.e. across all
#                       dates, to check the accuracy holds beyond the latest batch
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
MODE = sys.argv[2] if len(sys.argv) > 2 else "recent"

# keep the two sets in separate folders so a random pull never clobbers the
# recent one (or its review page). both are git-ignored (real member_ids).
OUT_DIR = os.path.join("eval_samples", "db" if MODE == "recent" else "db_random")
MANIFEST = os.path.join(OUT_DIR, "manifest.json")


def fetch_candidates(limit):
    """Parcels with an image and a clean numeric member_id (ground truth).

    We over-fetch (3x) so failed downloads or empty image arrays still leave
    enough good rows to reach `limit`. `recent` orders by newest; `random`
    samples uniformly across the whole table (a full scan+sort on ~1M rows,
    fine for a one-off), so the set spans all dates rather than the latest day.
    """
    order = "random()" if MODE == "random" else "p.created_at desc"
    conn = psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )
    cur = conn.cursor()
    cur.execute(
        f"""
        select p.parcel_id, p.images[1], u.member_id, p.created_at
        from cargo_parcels p
        join users u on u.id = p.user_id
        where p.images is not null
          and array_length(p.images, 1) > 0
          and u.member_id ~ '^[0-9]{{5,7}}$'
        order by {order}
        limit %s
        """,
        (limit * 3,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    candidates = fetch_candidates(N)
    print(f"fetched {len(candidates)} candidate rows; targeting {N} photos")

    manifest = []
    for parcel_id, url, member_id, created_at in candidates:
        if len(manifest) >= N:
            break
        fname = f"parcel_{parcel_id}.jpg"
        dest = os.path.join(OUT_DIR, fname)
        try:
            size = download(url, dest)
        except Exception as e:
            print(f"  skip parcel {parcel_id}: download failed ({type(e).__name__})")
            continue
        # tag the sample with its year-month as the "platform", so eval_platforms'
        # per-platform table becomes an accuracy-over-time breakdown.
        bucket = created_at.strftime("%Y-%m") if created_at else "unknown"
        manifest.append({"file": fname, "member_id": member_id, "platform": bucket})
        print(f"  ok  parcel {parcel_id}  member_id={member_id}  {bucket}  {size} bytes")

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nwrote {len(manifest)} entries to {MANIFEST}")
    print(f"run: modal run modal_app.py::eval_platforms --manifest {MANIFEST}")


if __name__ == "__main__":
    main()
