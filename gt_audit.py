import json
import sys

# offline audit of the decision log against ground truth. answers two questions
# the shipped eval table leaves open:
#   1. what would a marker-free auto-accept policy cost? (auto-accept every unique
#      db-valid run, dropping the warehouse-marker requirement that gates it now)
#   2. how many "misreads" are actually the model reading a real-but-different
#      client id — an adjacent-client collision, not an OCR failure the arbiter
#      could catch?
#
# no db and no gpu: the log already carries each transcript, the shipped decision,
# and the verified id. ground-truth ids come from parcel -> users.member_id, so
# every corrected_id is itself a real client; and the logged reason string tells
# us db-validity ("one db-valid digit run" == the logged member_id is in the db).
# that is enough to replay the counterfactual deterministically.

LOG = sys.argv[1] if len(sys.argv) > 1 else "decisions.jsonl"


def load_unique(path):
    # one row per photo, last write wins, so re-runs of the same photo don't
    # double-count.
    by_photo = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            by_photo[r["photo"]] = r
    return list(by_photo.values())


def main():
    rows = load_unique(LOG)

    # split off the routing/absent-id photos (no ground-truth id). the invariant
    # for these is simply: must never auto-accept.
    labeled = [r for r in rows if r.get("corrected_id")]
    no_id = [r for r in rows if not r.get("corrected_id")]

    n = len(labeled)
    print(f"log: {LOG}")
    print(f"unique photos: {len(rows)}  (with ground-truth id: {n}, no id: {len(no_id)})")
    print()

    # shipped policy, as logged.
    ship_accept = [r for r in labeled if r["decision"]["status"] == "accept"]
    ship_correct = [r for r in labeled if r["decision"].get("member_id") == r["corrected_id"]]
    ship_false = [r for r in ship_accept if r["decision"].get("member_id") != r["corrected_id"]]

    print("shipped policy (marker-gated auto-accept)")
    print(f"  auto-accepted:   {len(ship_accept)}/{n} ({len(ship_accept)/n:.0%})")
    print(f"  overall correct: {len(ship_correct)}/{n} ({len(ship_correct)/n:.0%})")
    print(f"  false-accept:    {len(ship_false)}")
    for r in ship_false:
        print(f"     {r['photo']}: read {r['decision']['member_id']} vs gt {r['corrected_id']}")
    print()

    # counterfactual: also auto-accept the manual db_scan cases that found exactly
    # one db-valid run (reason == "... one db-valid digit run"). the logged
    # member_id is that unique run; accepting it is the marker-free policy.
    extra = [
        r for r in labeled
        if r["decision"]["status"] == "manual"
        and "one db-valid digit run" in r["decision"].get("reason", "")
    ]
    extra_correct = [r for r in extra if r["decision"].get("member_id") == r["corrected_id"]]
    extra_false = [r for r in extra if r["decision"].get("member_id") != r["corrected_id"]]

    cf_accept = len(ship_accept) + len(extra)
    cf_false = len(ship_false) + len(extra_false)

    print("counterfactual: drop the marker requirement, accept any unique db-valid run")
    print(f"  newly auto-accepted: {len(extra)}  (correct {len(extra_correct)}, false {len(extra_false)})")
    print(f"  auto-accepted total: {cf_accept}/{n} ({cf_accept/n:.0%})")
    print(f"  false-accept total:  {cf_false}/{n} ({cf_false/n:.0%})")
    print()
    print("  false-accepts this would add (read a *different* real client's id):")
    for r in extra_false:
        print(f"     {r['photo']}: read {r['decision']['member_id']} vs gt {r['corrected_id']}")


if __name__ == "__main__":
    main()
