"""Before/after: split an eval set by whether the id was legibly captured.

Quantifies the framing lever. For each photo in a manifest, replay the logged
decision against ground truth and label the photo by an INPUT property — was the
member_id legibly *captured*? (its digits appear in the transcript, exactly or
within one digit) — judged before the decision, so it is not "did we get the final
answer right." Then it reports accuracy in each group:

  id legibly in frame   the id was presented to the camera (framing OK)
  id not in frame       the id never appears — wrong face / too small / cut off

The gap between the two is the value of fixing capture at the source. If the photo
files are present it also prints mean brightness per group (a capture-quality
proxy); brightness needs Pillow but is optional — the split runs without it.

Run:  python framing_split.py [manifest.json]
      default manifest: eval_samples/db/manifest.json
"""

import json
import os
import sys

from decision_log import read_decisions
from extraction import find_member_id_candidates

try:
    from PIL import Image
except ImportError:  # brightness is a nicety; the split works without Pillow
    Image = None


def within_one_digit(truth: str, run: str) -> bool:
    if len(run) == len(truth):
        return sum(a != b for a, b in zip(run, truth)) == 1
    if len(run) + 1 == len(truth):
        return any(truth[:i] + truth[i + 1:] == run for i in range(len(truth)))
    if len(truth) + 1 == len(run):
        return any(run[:i] + run[i + 1:] == truth for i in range(len(run)))
    return False


def captured(truth: str, transcript: str) -> bool:
    # was the id legibly presented? its digits show up exactly or within one edit.
    if not truth:
        return False
    flat = (transcript or "").replace(" ", "")
    if truth in flat:
        return True
    return any(within_one_digit(truth, run) for run in find_member_id_candidates(transcript or ""))


def brightness(path: str):
    # mean luminance 0-255 on a downscaled grayscale copy; a capture-quality proxy.
    if Image is None or not os.path.exists(path):
        return None
    try:
        img = Image.open(path).convert("L")
        img.thumbnail((128, 128))
        px = list(img.getdata())
        return sum(px) / len(px)
    except Exception:
        return None


def summarize(name: str, rows: list[dict]):
    n = len(rows)
    if not n:
        print(f"{name:<24} 0 photos")
        return
    c = sum(r["correct"] for r in rows)
    a = sum(r["accepted"] for r in rows)
    fa = sum(r["false_accept"] for r in rows)
    br = [r["bright"] for r in rows if r["bright"] is not None]
    mb = f"{sum(br) / len(br):.0f}/255" if br else "n/a"
    print(f"{name:<24} n={n:<3} correct={c}/{n} ({100 * c // n}%)   "
          f"auto-accept={a}/{n} ({100 * a // n}%)   false-accept={fa}   mean-brightness={mb}")


def main():
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join("eval_samples", "db", "manifest.json")
    base = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, encoding="utf-8") as f:
        manifest = {m["file"]: m for m in json.load(f)}

    recs = {r.get("photo", ""): r for r in read_decisions()}

    groups = {"in": [], "out": []}
    for file, m in manifest.items():
        r = recs.get(file)
        if not r:
            continue
        truth = m.get("member_id")
        d = r["decision"]
        got = d.get("member_id")
        accepted = d.get("status") == "accept"
        row = {
            "file": file, "truth": truth, "got": got,
            "correct": got == truth, "accepted": accepted,
            "false_accept": accepted and got != truth,
            "bright": brightness(os.path.join(base, file)),
        }
        groups["in" if captured(truth, r.get("transcript", "")) else "out"].append(row)

    allrows = groups["in"] + groups["out"]
    if not allrows:
        print("no scored photos — run eval_platforms on this manifest first.")
        return

    print("=== before (all photos, current mixed framing) ===")
    summarize("all photos", allrows)
    print("\n=== split by whether the id was legibly captured ===")
    summarize("id legibly in frame", groups["in"])
    summarize("id not in frame", groups["out"])

    miss = [r for r in groups["in"] if not r["correct"]]
    if miss:
        print(f"\nceiling — the {len(miss)} in-frame miss(es) are single-digit decoys:")
        for r in miss:
            tag = "FALSE-ACCEPT" if r["false_accept"] else "held-manual"
            print(f"    {r['file']:<24} truth={r['truth']}  got={r['got']}  {tag}")


if __name__ == "__main__":
    main()
