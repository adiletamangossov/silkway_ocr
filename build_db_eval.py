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

# how many labeled photos to assemble (default 10)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10

OUT_DIR = os.path.join("eval_samples", "db")
MANIFEST = os.path.join(OUT_DIR, "manifest.json")


def fetch_candidates(limit):
    """Recent parcels with an image and a clean numeric member_id (ground truth).

    We over-fetch (3x) so failed downloads or empty image arrays still leave
    enough good rows to reach `limit`.
    """
    conn = psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )
    cur = conn.cursor()
    cur.execute(
        """
        select p.parcel_id, p.images[1], u.member_id
        from cargo_parcels p
        join users u on u.id = p.user_id
        where p.images is not null
          and array_length(p.images, 1) > 0
          and u.member_id ~ '^[0-9]{5,7}$'
        order by p.created_at desc
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
    for parcel_id, url, member_id in candidates:
        if len(manifest) >= N:
            break
        fname = f"parcel_{parcel_id}.jpg"
        dest = os.path.join(OUT_DIR, fname)
        try:
            size = download(url, dest)
        except Exception as e:
            print(f"  skip parcel {parcel_id}: download failed ({type(e).__name__})")
            continue
        manifest.append({"file": fname, "member_id": member_id, "platform": "db"})
        print(f"  ok  parcel {parcel_id}  member_id={member_id}  {size} bytes")

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nwrote {len(manifest)} entries to {MANIFEST}")
    print(f"run: modal run modal_app.py::eval_platforms --manifest {MANIFEST}")


if __name__ == "__main__":
    main()
