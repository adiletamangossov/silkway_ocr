"""A/B: PaddleOCR vs Qwen3-VL as the transcribe engine, same DB-arbiter pipeline.

The pipeline is transcribe -> resolve_member_id -> DB validate. Only the transcribe
box differs, so this swaps in PaddleOCR (a fast, Chinese-strong classic OCR engine,
CPU) and compares it head to head against the current 8B VLM on the labeled eval
photos, on BOTH accuracy and latency.

Fairness: every PaddleOCR transcript AND every already-logged Qwen transcript is run
through the SAME current resolve_member_id (same DB, same zero-misdelivery policy),
so the only variable is the OCR text. Qwen transcripts come from the decision log
(the recent 50-photo eval), so we don't pay to re-run the slow VLM.

Run:  modal run paddle_ab.py::ab [--manifest eval_samples/db/manifest.json] [--limit N]
"""

import modal

# the PP-OCR models (Baidu's PaddleOCR) run here via ONNX Runtime (RapidOCR)
# instead of paddlepaddle: same models / accuracy, but a portable CPU runtime with
# no SIGILL on heterogeneous cloud CPUs and no model download (weights ship in the
# wheel, so cold start is quick). apt libs are for opencv.
paddle_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libgl1", "libglib2.0-0")
    .pip_install("rapidocr-onnxruntime", "pillow", "numpy<2")
)

app = modal.App("silkway-paddle-ab")


@app.cls(image=paddle_image, cpu=4, scaledown_window=300, timeout=600)
class PaddleOCR:
    @modal.enter()
    def load(self):
        from rapidocr_onnxruntime import RapidOCR

        # raise the detection resolution cap (default ~736) so a small member_id in
        # a high-res photo isn't downscaled below the detection threshold.
        self.engine = RapidOCR(det_limit_side_len=1920, det_limit_type="max")

    @modal.method()
    def transcribe(self, image_bytes: bytes) -> str:
        import io

        import numpy as np
        from PIL import Image

        img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        result, _ = self.engine(img)  # -> [[box, text, score], ...] or None

        # join every detected line into one transcript, same shape resolve expects.
        lines = [item[1] for item in (result or [])]
        return "\n".join(lines)


@app.local_entrypoint()
def dump(files: str = "parcel_2671717.jpg,parcel_2671716.jpg,parcel_2671710.jpg"):
    # print the raw PaddleOCR transcript for a few photos, to see what it reads
    # (big printed text vs the small member_id) and diagnose the misses.
    import os

    ocr = PaddleOCR()
    for f in files.split(","):
        path = os.path.join("eval_samples", "db", f)
        txt = ocr.transcribe.remote(open(path, "rb").read())
        print("=" * 50, f, f"({len(txt)} chars)")
        print(txt or "(empty)")


@app.local_entrypoint()
def ab(manifest: str = "eval_samples/db/manifest.json", limit: int = 0):
    import json
    import os
    import time

    from dotenv import load_dotenv

    load_dotenv()

    from validation import PostgresUserIDStore, resolve_member_id
    from evaluation import score
    from decision_log import read_decisions

    base = os.path.dirname(os.path.abspath(manifest))
    samples = json.load(open(manifest, encoding="utf-8"))
    if limit:
        samples = samples[:limit]

    store = PostgresUserIDStore()
    ocr = PaddleOCR()

    # latest logged Qwen transcript per photo (from the recent eval run).
    qwen_log = {r["photo"]: r for r in read_decisions()}

    paddle_rows, qwen_rows, lat = [], [], []
    print(f"{'photo':<22} {'exp':<8} {'paddle':<8} {'q-wen':<8} {'p-lat':>7}")
    for s in samples:
        path = os.path.join(base, s["file"])
        if not os.path.exists(path):
            continue
        expected = s.get("member_id")
        platform = s.get("platform", "?")

        t0 = time.time()
        p_txt = ocr.transcribe.remote(open(path, "rb").read())
        dt = time.time() - t0
        lat.append(dt)

        p_dec = resolve_member_id(p_txt, store)
        paddle_rows.append({"platform": platform, **score(expected, p_dec)})

        # re-resolve the logged Qwen transcript with the SAME current pipeline.
        q = qwen_log.get(s["file"])
        q_id = None
        if q and q.get("transcript"):
            q_dec = resolve_member_id(q["transcript"], store)
            qwen_rows.append({"platform": platform, **score(expected, q_dec)})
            q_id = q_dec.get("member_id")

        print(f"{s['file']:<22} {str(expected):<8} {str(p_dec.get('member_id')):<8} "
              f"{str(q_id):<8} {dt:>6.2f}s")

    def tally(name, rows):
        n = len(rows)
        if not n:
            print(f"{name}: no rows")
            return
        c = sum(r["correct"] for r in rows)
        a = sum(r["accepted"] for r in rows)
        fa = sum(r["false_accept"] for r in rows)
        print(f"{name:<10} n={n:<3} correct={c}/{n} ({100*c//n}%)   "
              f"auto-accept={a}/{n} ({100*a//n}%)   false-accept={fa}")

    print("\n=== accuracy (same resolver + db for both) ===")
    tally("Qwen3-VL", qwen_rows)
    tally("PaddleOCR", paddle_rows)

    if lat:
        warm = lat[1:] or lat  # drop the first (cold) call
        print(f"\n=== latency (PaddleOCR transcribe, CPU) ===")
        print(f"first call (cold): {lat[0]:.2f}s   warm median: {sorted(warm)[len(warm)//2]:.2f}s   "
              f"warm mean: {sum(warm)/len(warm):.2f}s")
        print("Qwen3-VL for reference: ~25s warm, ~60s cold (measured on L4).")
