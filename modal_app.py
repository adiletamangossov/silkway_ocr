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


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------
# A deployed web service: POST a courier-sticker photo, get the resolved
# member_id back. Runs the whole pipeline in the cloud so the caller makes one
# call and receives the final decision — transcribe on the GPU container above,
# then resolve against the live client db right here.
#
# This is a separate CPU function, not a method on QwenOCR, on purpose: the GPU
# container (and its image) stay exactly as they are, so the existing local
# entrypoints and the deployed model are unaffected. The web layer scales to
# zero and only forwards image bytes to the GPU over Modal's internal network.

# the web layer needs psycopg (db validation now runs in-cloud, not on a laptop)
# and fastapi (the endpoint + multipart upload). added in a layer AFTER the heavy
# torch install so the GPU image cache is untouched. our pure-python parsing and
# validation modules are shipped into the container so resolve_member_id can run.
web_image = (
    image.pip_install("psycopg[binary]", "fastapi[standard]")
    .add_local_python_source("extraction", "validation", "decision_sink")
)

# db credentials + the endpoint bearer token, kept out of code in a Modal secret.
# create it once against your account:
#   modal secret create silkway-secrets \
#     DB_HOST=... DB_PORT=4444 DB_USER=... DB_PASSWORD=... DB_NAME=... API_TOKEN=...
# the local entrypoints below still read the same values from .env; only the
# deployed endpoint reads them from this secret. API_TOKEN is optional — if it is
# absent the endpoint runs open (fine behind a private gateway); if present, every
# request must carry `Authorization: Bearer <token>`.
db_secret = modal.Secret.from_name("silkway-secrets")


@app.function(image=web_image, secrets=[db_secret], timeout=600)
@modal.concurrent(max_inputs=100)  # one CPU container fields many requests at once
@modal.asgi_app()
def web():
    import asyncio
    import os

    from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

    # imported here (in the container), where the modules and psycopg exist.
    from validation import PostgresUserIDStore, resolve_member_id
    from decision_sink import PostgresDecisionSink

    api = FastAPI(
        title="SilkWay OCR",
        summary="Read a client's member_id off a courier-sticker photo.",
    )

    # durable log of every decision, written to an ocr_decisions table in the same
    # db (the container filesystem is ephemeral, so a file log would not survive).
    # create the table once at container start; best-effort so a transient db hiccup
    # here never stops the endpoint from serving.
    sink = PostgresDecisionSink()
    try:
        sink.ensure_schema()
    except Exception as e:  # noqa: BLE001
        print(f"ocr_decisions schema init failed (non-fatal): {e}")

    def require_token(authorization: str | None):
        # enforce the bearer token only when one is configured, so a first deploy
        # works before you wire up API_TOKEN. once set, it is mandatory.
        expected = os.environ.get("API_TOKEN")
        if expected and authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    @api.get("/health")
    def health():
        # cheap liveness check that never touches the gpu.
        return {"status": "ok", "model": MODEL_ID}

    @api.post("/recognize")
    async def recognize(
        file: UploadFile = File(...),
        platform: str | None = Form(default=None),
        authorization: str | None = Header(default=None),
    ):
        require_token(authorization)

        image_bytes = await file.read()
        if not image_bytes:
            raise HTTPException(status_code=400, detail="empty image file")

        # transcribe on the GPU container — .aio so this async request handler does
        # not block the event loop while the model runs.
        transcript = await QwenOCR().transcribe.remote.aio(image_bytes, TRANSCRIBE_PROMPT)

        def resolve_and_log():
            # resolve the member_id against the live db (the db lookup is the
            # confidence signal, not the model), then record the decision. both are
            # blocking psycopg calls, so this runs in a worker thread — see below.
            decision = resolve_member_id(transcript, PostgresUserIDStore())
            try:
                # corrected_id is unknown now; a human backfills it from the manual
                # queue later. logging is best-effort: never fail the request over it.
                sink.log_decision(file.filename, transcript, decision, platform=platform)
            except Exception as e:  # noqa: BLE001
                print(f"decision log failed (non-fatal): {e}")
            return decision

        # offload the blocking db work off the event loop so concurrent requests
        # (max_inputs above) are not serialized behind each other's db round trips.
        decision = await asyncio.to_thread(resolve_and_log)

        # the full resolver decision (status / member_id / confidence / source /
        # reason / candidates) plus the raw transcript, which the manual queue and
        # any later audit both want to see.
        return {"transcript": transcript, **decision}

    return api


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


# A/B test the brightness preprocess. run with:
#   modal run modal_app.py::ab_preprocess [--manifest ...] [--limit N]
# the real photos are badly underexposed, so this measures the lift from
# brightening each image before the VLM sees it. every photo is transcribed TWICE
# in one warm container — raw and preprocessed — and both are scored against the
# same ground truth, so the two arms differ only by the preprocess. results are
# printed and written to preprocess_ab.jsonl; the shipped decisions.jsonl (the
# baseline) is left untouched.
@app.local_entrypoint()
def ab_preprocess(manifest: str = "eval_samples/db/manifest.json", limit: int = 0):
    import json
    import os

    from dotenv import load_dotenv

    load_dotenv()

    from validation import PostgresUserIDStore, resolve_member_id
    from evaluation import score, aggregate, format_table
    from preprocess import enhance_bytes

    base = os.path.dirname(os.path.abspath(manifest))
    with open(manifest, encoding="utf-8") as f:
        samples = json.load(f)
    if limit:
        samples = samples[:limit]

    store = PostgresUserIDStore()
    ocr = QwenOCR()

    # rows per arm for the aggregate table, plus a running count of photos the
    # preprocess flipped from wrong->right (won) or right->wrong (lost).
    arms = {"raw": [], "pre": []}
    won, lost = [], []
    out = open("preprocess_ab.jsonl", "w", encoding="utf-8")

    for s in samples:
        path = os.path.join(base, s["file"])
        if not os.path.exists(path):
            print(f"skip (missing): {s['file']}")
            continue

        raw_bytes = open(path, "rb").read()
        pre_bytes = enhance_bytes(raw_bytes)

        expected = s.get("member_id")
        platform = s.get("platform", "?")

        outcomes = {}
        for arm, image_bytes in (("raw", raw_bytes), ("pre", pre_bytes)):
            transcript = ocr.transcribe.remote(image_bytes, TRANSCRIBE_PROMPT)
            decision = resolve_member_id(transcript, store)
            result = score(expected, decision)
            arms[arm].append({"platform": platform, **result})
            outcomes[arm] = {"decision": decision, "result": result, "transcript": transcript}

        raw_ok = outcomes["raw"]["result"]["correct"]
        pre_ok = outcomes["pre"]["result"]["correct"]
        flip = "won " if (pre_ok and not raw_ok) else "lost" if (raw_ok and not pre_ok) else "same"
        if flip == "won ":
            won.append(s["file"])
        elif flip == "lost":
            lost.append(s["file"])

        out.write(json.dumps({"file": s["file"], "expected": expected, **outcomes}, ensure_ascii=False) + "\n")
        print(
            f"{flip}  {s['file']:<22} exp={str(expected):<8} "
            f"raw={str(outcomes['raw']['decision'].get('member_id')):<8}"
            f"({outcomes['raw']['decision']['status'][:3]})  "
            f"pre={str(outcomes['pre']['decision'].get('member_id')):<8}"
            f"({outcomes['pre']['decision']['status'][:3]})"
        )

    out.close()
    if not arms["raw"]:
        print("no samples evaluated.")
        return

    for arm in ("raw", "pre"):
        groups, total = aggregate(arms[arm])
        print(f"\n=== {arm} ===")
        print(format_table(groups, total))

    print(f"\npreprocess flipped {len(won)} wrong->right, {len(lost)} right->wrong")
    if won:
        print("  won:  " + ", ".join(won))
    if lost:
        print("  lost: " + ", ".join(lost))
    print("per-photo detail written to preprocess_ab.jsonl")
