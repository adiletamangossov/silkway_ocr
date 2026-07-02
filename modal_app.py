import modal

# the open-weights model we self-host
MODEL_ID = "Qwen/Qwen3-VL-8B-Instruct"

# GPU-side dependencies. these are installed into the Modal container image,
# not on your laptop. transformers must be recent enough to know Qwen3-VL.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch",
        "torchvision",
        "transformers>=4.57",
        "accelerate",
        "pillow",
    )
    # point HuggingFace at the mounted volume so weights are cached, not
    # re-downloaded on every cold start
    .env({"HF_HOME": "/cache"})
)

# persistent volume that survives container shutdown; holds the model weights
hf_cache = modal.Volume.from_name("silkway-hf-cache", create_if_missing=True)

app = modal.App("silkway-ocr")

# the read-only transcription prompt, shared by every entrypoint. tell the model
# to read, never to translate or guess, so the deterministic parsing downstream
# sees exactly what is printed on the label.
TRANSCRIBE_PROMPT = (
    "Transcribe all text on this shipping label exactly as printed, "
    "including every Chinese character and every digit, line by line. "
    "Do not translate, summarize, reorder, or correct anything. "
    "If a character is unclear, output your best literal reading of what is visible."
)


@app.cls(
    image=image,
    gpu="L4",                 # single 24GB GPU is enough for an 8B VLM
    volumes={"/cache": hf_cache},
    scaledown_window=300,     # keep the container warm 5 min after the last call
    timeout=600,
    # reserve host RAM for the cold-start weight load. the 8B shards stream
    # through CPU memory before landing on the GPU, and the default allotment
    # was tight enough to OOM-kill (exit 137) a container mid-load; Modal retried
    # and the next one survived, but reserving 16GB removes that flaky failure.
    memory=16384,
)
class QwenOCR:
    @modal.enter()
    def load(self):
        # runs once when a container starts, before it serves any request
        import torch  # noqa: F401  (imported so failures surface here)
        from transformers import Qwen3VLForConditionalGeneration, AutoProcessor

        self.model = Qwen3VLForConditionalGeneration.from_pretrained(
            MODEL_ID, dtype="auto", device_map="auto"
        )
        self.processor = AutoProcessor.from_pretrained(MODEL_ID)

    @modal.method()
    def transcribe(self, image_bytes: bytes, prompt: str) -> str:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": img},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        inputs = self.processor.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        ).to(self.model.device)

        # greedy decoding: deterministic, no creative "fixing" of digits.
        # repetition_penalty + no_repeat_ngram_size stop the degenerate loops the
        # model falls into on dark, low-information photos, where it otherwise
        # invents a recipient block and repeats it until max_new_tokens.
        generated = self.model.generate(
            **inputs,
            max_new_tokens=512,
            do_sample=False,
            repetition_penalty=1.1,
            no_repeat_ngram_size=4,
        )

        text = self.processor.batch_decode(
            generated[:, inputs.input_ids.shape[-1]:],
            skip_special_tokens=True,
        )[0]
        return text.strip()


# lets us test from the terminal:
#   modal run modal_app.py --image-path label.jpg [--platform taobao]
@app.local_entrypoint()
def main(image_path: str, platform: str = None):
    # load db credentials from the local .env file into the environment, so
    # PostgresUserIDStore can read them. runs locally, never in the container.
    from dotenv import load_dotenv

    load_dotenv()

    # imported here, not at module top: validation runs locally after the
    # remote call, so the gpu container never needs this module.
    from validation import PostgresUserIDStore, resolve_member_id
    from decision_log import log_decision, log_path

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    text = QwenOCR().transcribe.remote(image_bytes, TRANSCRIBE_PROMPT)
    print(text)

    # resolve the member id: marker-anchored parse first, then a db-assisted
    # fallback if the marker is missing. the db lookup is the confidence signal.
    store = PostgresUserIDStore()
    decision = resolve_member_id(text, store)
    print("\n--- resolution ---")
    print(decision)

    # append the run to the decision log so real usage accumulates a labeled
    # dataset and a per-platform error record. the verified id is unknown here
    # (a human fills it in later for manual cases), so corrected_id stays None.
    log_decision(image_path, text, decision, platform=platform)
    print(f"logged to {log_path()}")


# end-to-end check over the real sample photos against the live db. run with:
#   modal run modal_app.py::e2e
# one warm container handles all three images. each case carries the behaviour
# we expect from the project notes; we assert the invariant that must hold (never
# auto-accept a label that has no real id) and print the full decision to eyeball.
@app.local_entrypoint()
def e2e():
    from dotenv import load_dotenv

    load_dotenv()

    from validation import PostgresUserIDStore, resolve_member_id

    # (file, what the photo is, the invariant that must hold)
    cases = [
        (
            "image_silkway.jpeg",
            "clean sample, 首都波...号 marker present",
            "marker path resolves an id (accept if that id is a real client)",
        ),
        (
            "problem_photo.jpg",
            "domestic routing sticker, carries NO client id",
            "must NOT accept; nothing real to resolve",
        ),
        (
            "hided.jpg",
            "China Post label, id digit covered by red sorting-pen ink",
            "must NOT accept; obscured digit is unrecoverable -> manual",
        ),
    ]

    store = PostgresUserIDStore()
    ocr = QwenOCR()

    results = []
    for filename, what, expectation in cases:
        with open(filename, "rb") as f:
            image_bytes = f.read()

        transcript = ocr.transcribe.remote(image_bytes, TRANSCRIBE_PROMPT)
        decision = resolve_member_id(transcript, store)

        # the one hard rule across every case: a label with no genuine member id
        # must never be auto-accepted. the marker sample may legitimately accept.
        accepted = decision["status"] == "accept"
        must_not_accept = filename != "image_silkway.jpeg"
        ok = not (accepted and must_not_accept)

        results.append((filename, ok))

        print("=" * 70)
        print(f"{filename}  —  {what}")
        print(f"expect: {expectation}")
        print("- transcript ".ljust(70, "-"))
        print(transcript)
        print("- decision ".ljust(70, "-"))
        print(decision)
        print(f"invariant held: {ok}")
        print()

    print("#" * 70)
    for filename, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {filename}")


# per-platform accuracy harness. run with:
#   modal run modal_app.py::eval_platforms [--manifest eval_manifest.json]
# reads a manifest of labeled photos, runs the full pipeline against the live db,
# scores each against ground truth, and prints accuracy broken down by origin
# platform (taobao / pinduoduo / poizon / ...). every result is also appended to
# the decision log, so the eval set doubles as labeled training data.
@app.local_entrypoint()
def eval_platforms(manifest: str = "eval_manifest.json"):
    import json
    import os

    from dotenv import load_dotenv

    load_dotenv()

    from validation import PostgresUserIDStore, resolve_member_id
    from decision_log import log_decision
    from evaluation import score, aggregate, format_table

    # photo paths in the manifest are relative to the manifest's own folder.
    base = os.path.dirname(os.path.abspath(manifest))
    with open(manifest, encoding="utf-8") as f:
        samples = json.load(f)

    store = PostgresUserIDStore()
    ocr = QwenOCR()

    rows = []
    for s in samples:
        image_path = os.path.join(base, s["file"])
        if not os.path.exists(image_path):
            print(f"skip (missing file): {s['file']}")
            continue

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        transcript = ocr.transcribe.remote(image_bytes, TRANSCRIBE_PROMPT)
        decision = resolve_member_id(transcript, store)

        expected = s.get("member_id")
        platform = s.get("platform", "?")
        result = score(expected, decision)
        rows.append({"platform": platform, **result})

        # we hold ground truth here, so record it as the verified id.
        log_decision(image_path, transcript, decision, platform=platform, corrected_id=expected)

        mark = "ok  "
        if not result["correct"]:
            mark = "MISS"
        if result["false_accept"]:
            mark = "FALSE-ACCEPT"
        print(
            f"{mark}  {s['file']:<22} platform={platform:<10} "
            f"expected={str(expected):<8} got={str(decision.get('member_id')):<8} "
            f"status={decision['status']}"
        )

    if not rows:
        print("no samples evaluated. add photos and entries to the manifest.")
        return

    groups, total = aggregate(rows)
    print()
    print(format_table(groups, total))
