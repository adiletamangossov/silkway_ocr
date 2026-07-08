"""Categorize why the pipeline missed on an eval set — reproducible, no gpu/db.

For every photo in a manifest, replay the logged decision (from the decision log)
against ground truth and bucket each miss, so we can tell a capture problem apart
from a model or resolver problem:

  ABSENT           the id's digits never appear in the transcript — the capture
                   didn't present the id (wrong label face / too small / cut off)
  WITHIN-1-DIGIT   a digit run one edit away from the truth was read (a misread,
                   correctly held by the wildcard tier rather than auto-accepted)
  RESOLVER-MISSED  the true id IS in the transcript but the decision missed it.
                   This bucket staying at 0 means there is no parsing/logic gap —
                   every miss is a genuine OCR read failure.

Pairs with build_db_eval.py (builds the labeled set) and eval_platforms (produces
the decisions). Reads only the decision log + manifest, so it re-runs offline.

Run:  python analyze_misses.py [manifest.json]
      default manifest: eval_samples/db/manifest.json
"""

import json
import os
import sys

from decision_log import read_decisions
from extraction import find_member_id_candidates


def within_one_digit(truth: str, run: str) -> bool:
    # true if `run` is one edit from `truth`: a single substituted digit (same
    # length), or one digit dropped / added (length off by one). mirrors the
    # misread + obscured-digit cases the wildcard tier reasons about.
    if len(run) == len(truth):
        return sum(a != b for a, b in zip(run, truth)) == 1
    if len(run) + 1 == len(truth):
        return any(truth[:i] + truth[i + 1:] == run for i in range(len(truth)))
    if len(truth) + 1 == len(run):
        return any(run[:i] + run[i + 1:] == truth for i in range(len(run)))
    return False


def categorize(truth: str, transcript: str) -> str:
    flat = (transcript or "").replace(" ", "")
    runs = find_member_id_candidates(transcript or "")
    if truth and truth in flat:
        return "RESOLVER-MISSED"
    if truth and any(within_one_digit(truth, run) for run in runs):
        return "WITHIN-1-DIGIT"
    return "ABSENT"


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("eval_samples", "db", "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = {m["file"]: m for m in json.load(f)}

    # latest logged decision per photo (the log accumulates every run)
    recs = {r.get("photo", ""): r for r in read_decisions()}

    buckets = {"ABSENT": [], "WITHIN-1-DIGIT": [], "RESOLVER-MISSED": []}
    scored = 0
    for file, m in manifest.items():
        r = recs.get(file)
        if not r:
            continue
        scored += 1
        truth = m.get("member_id")
        got = r["decision"].get("member_id")
        if got == truth:
            continue  # correct read, not a miss
        cat = categorize(truth, r.get("transcript", ""))
        buckets[cat].append((file, truth, got))

    misses = sum(len(v) for v in buckets.values())
    print(f"scored {scored} photos; {misses} miss(es)\n")
    for cat in ("ABSENT", "WITHIN-1-DIGIT", "RESOLVER-MISSED"):
        rows = buckets[cat]
        print(f"{cat:<16} {len(rows)}")
        for file, truth, got in rows:
            print(f"    {file:<24} truth={truth}  got={got}")
    if not buckets["RESOLVER-MISSED"]:
        print("\nno resolver-missed cases: every miss is an OCR read failure, not a parsing gap.")


if __name__ == "__main__":
    main()
