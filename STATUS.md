# SilkWay OCR — Status

_Last updated: 2026-07-03_

## What it does

Reads a client's **member_id** off a courier sticker photo and auto-fills it, so
warehouse specialists stop keying it by hand.

**Pipeline:** phone photo → Qwen3-VL-8B on Modal GPU transcribes all text →
deterministic Python extracts digit runs → **Postgres `users.member_id` is the
arbiter**. VLMs have no reliable confidence and can hallucinate digits, so a DB
match is the confidence signal — not the model.

## Real-data eval (2026-07-02 – 07-03)

Photos + ground truth come straight from the prod DB: `cargo_parcels.images` are
the courier photos, and each parcel's `user_id → users.member_id` is the verified
member_id (the warehouse already matched the parcel to that client). `build_db_eval.py`
downloads N labeled photos; `eval_platforms` scores the full pipeline against them.

**78 real (old, pre-high-visibility) photos** — full decision log, deduped
(`gt_audit.py` replays it against ground truth):

| Metric | **Shipped (marker-gated)** |
|---|---|
| Auto-accepted | **27/78 (35%)** |
| — correct | 26 |
| — **false-accept** | **1** (`912183`→`912185`) |
| Overall correct read | 44/78 (56%) |
| Routed to manual | 51/78 |

Decision: **ship fuzzy auto-accept unattended** — 35% hands-off, ~1% false-accept
(a single-digit misread that lands on another real client). The DB-arbiter still
blocks all non-member-id garbage; the residual error is adjacent-client digit
collisions only.

**The marker gate is load-bearing — quantified (2026-07-03).** The biggest manual
bucket (20/51) is *"unique db-valid digit run present, but no warehouse marker."*
Dropping the marker requirement and auto-accepting those unique runs would lift
auto-accept 35% → **60%**, but false-accept jumps 1 → **6/78 (8%)**: 5 of the 20
read a *different real client's* id (`971218`→`971318`, `960860`→`960800`,
`966045`→`964945`, `900372`→`903372`, `902328`→`907378`). 6× the error for +25pp
coverage is not worth it; **keep the marker gate**.

**Ground truth ≠ printed sticker in some cases (2026-07-03).** `parcel_2654600.jpg`
plainly prints `SA-903372-2` / `用户ID: 903372` — the model read `903372`
*correctly off the label*, but the DB link (`parcel.user_id → users.member_id`)
says `900372`. So a slice of the "misreads" above are the **printed courier label
disagreeing with the DB linkage** (mis-printed label or stale link), not OCR
error. Two consequences: (a) our 56% "correct read" **understates OCR quality** —
the model faithfully transcribes stickers our labels then mark wrong; (b) it does
**not** rescue marker-free accept — whether the sticker or the link is wrong,
accepting that run still lands on the wrong client, so the gate stays regardless.

**Brightness preprocess measured — does NOT help, not shipped (2026-07-03).**
Controlled A/B via `modal_app.py::ab_preprocess`: each of the 50 db photos
transcribed twice in one container — raw vs brightened (`preprocess.py`,
autocontrast + adaptive gamma) — scored against the same ground truth.

| Arm | Correct | Auto-accept | False-accept |
|---|---|---|---|
| raw | 27/50 | 18/50 | 1 |
| preprocessed | 26/50 | 18/50 | 1 |

3 photos flipped wrong→right, 4 right→wrong — they cancel (net −1 correct, accept
and false-accept unchanged). Mechanism: brightening **perturbs borderline id-digit
reads**, fixing some misreads (`964945`→`966045`) and creating others (`972511`→
an inserted `8`→None). Not washout — a human finds the brightened frame far more
legible (`parcel_2654600` goes from near-black to crisp), but the model already
reads those pixels; the residual digit ambiguity is inherent to the low-quality
capture, and global tone-mapping just reshuffles the guess. **Do not ship
preprocessing**; the real lever stays high-visibility capture at source.
`preprocess.py` + the A/B harness are kept to re-test on high-visibility photos.
Crop left off: at mean-luminance ~12/255 the sticker isn't the brightest region,
so threshold-crop can't isolate it (would risk cropping out the id).

## Recent-photo eval (50, 2026-07-08)

First `eval_platforms` run on the **newest** parcels (all created 2026-07, freshest
captures = best proxy for the agreed high-visibility framing), built with
`build_db_eval.py 50`:

| Metric | **Recent 50** | Old baseline (pre-framing) |
|---|---|---|
| Correct read | **30/50 (60%)** | 26/50 (52%) |
| Auto-accepted | **20/50 (40%)** | 18/50 (36%) |
| **False-accept** | **1** | 1 |

A modest lift (+8pp correct, +4pp accept), **not** the jump full high-visibility
framing would give. The lone false-accept is the known fuzzy-marker decoy:
`parcel_2671738` truth `964556` read as `964506` (single `5→0` misread that is
itself a real client, marker present → `marker_fuzzy` auto-accept). ~2%, as before.

**Root cause of the misses — dug into the 20 (18 `None` + 1 wrong held + the 1 FA).**
Of the 18 `None` cases: **13/18 the id digits were never read at all** (no run
resembling the truth in the transcript — often only the printed courier hotline
like `95311`/`95720` came through, not the member_id), **5/18 were read one digit
off** and correctly held (wildcard couldn't disambiguate in the dense range), and
crucially **0/18 had the true id present-but-missed by the resolver**. So every
miss is a genuine OCR *read* failure, not a parsing/logic gap — and most (13/18)
are the id region simply being illegible in the capture, even though the warehouse
marker/address around it read fine. Confirms the standing conclusion: the lever is
**capture legibility of the id region at source**, not the model or the resolver.
The safety invariant held (19/20 wrong outcomes went to manual).

**Visual confirmation — viewed 4 of the `None` photos directly (2026-07-08).** All
four are the *same workstation*: a wide, underexposed overhead shot of a dark desk
with the parcel dropped in the bottom-right corner at ~10–15% of the frame. The id
wasn't read because it was never properly presented to the camera: `2671718` —
parcel tiny + dark; `2671699` — only the ZTO *routing* face shows (`广州转 航达
B.01`), receiver block not facing the camera; `2671761` — small screw-pack, only
the merchant product label (`R888`) visible, no courier sticker in frame;
`2671722` — parcel cut off by the frame edge, receiver block off-photo. So the
agreed high-visibility framing is **not adopted at this station** — these are wide
desk shots with the parcel incidental. Fix is operational, not model: fill the
frame with the receiver-address label (`…首都波XXXXXX号`), photograph the receiver
face (not routing/merchant), and add task lighting. No prompt/crop/fine-tune helps
a parcel whose id is off-frame or a few dozen pixels wide.

**Before/after — the framing lever quantified (2026-07-08).** Split the same 50
photos by whether the id was legibly *captured* (its digits appear in the
transcript, exactly or within one digit — a capture property judged before the
decision, so not "did we get it right"):

| Group | n | correct | auto-accept | false-accept | mean brightness |
|---|---|---|---|---|---|
| All 50 (before, mixed framing) | 50 | 30 (60%) | 20 (40%) | 1 | 14/255 |
| id legibly in frame | 37 | **30 (81%)** | **20 (54%)** | 1 | 14/255 |
| id never presented to camera | 13 | 0 (0%) | 0 (0%) | 0 | 13/255 |

The whole 60%→81% gap is capture: 13/50 (26%) never showed the id to the camera
(guaranteed 0%); on the 37 that did, the pipeline already reads 81% correct / 54%
hands-off *on these same dark photos*. Projected if all were framed like the
in-frame group: ~40/50 correct, ~27/50 auto-accepted — **+21pp accuracy, +14pp
automation from an operational change alone**. Both groups are equally dark (14 vs
13 /255), so the differentiator is **label presentation** (id large, receiver face,
not cut off), not exposure. Ceiling: the 7/37 in-frame misses are all single-digit
decoy misreads the dense db can't disambiguate (1 became the false-accept) — the
real model/db limit, and the only thing a crop/higher-res/fine-tune lever could
touch, worth far less than the framing fix.

**Accept policy switched to zero-misdelivery (2026-07-08).** The `marker_fuzzy`
tier (mangled marker + a unique db-valid `号`-run) was the only path that could
auto-accept a wrong id — a clean single-digit misread there can be a real adjacent
client the db can't distinguish — and it produced the lone false-accept in every
run. It now **prefills the manual queue for a one-click confirm instead of
auto-accepting** (`validation.py:MARKER_FUZZY_AUTOACCEPT = False`, one line to
revert). The exact-marker path (`首都波…号` read cleanly) still auto-accepts. Net:
**auto-accept false-accepts → 0**; most of the old ~40% hands-off becomes
prefilled one-click confirms (still far faster than typing). Verified live: the
previous false-accept photo now returns `manual` with the guess prefilled; the
clean exact-marker sample still auto-accepts. The earlier eval tables above were
measured under the old unattended mode — their `auto-accept` columns now read as
`accept + one-click-confirm`, and their single false-accepts as 0.

## PaddleOCR A/B — speed vs accuracy (2026-07-09)

Latency of the deployed Qwen endpoint measured ~25s warm / ~60s cold (external
service needs 2–3s). Since the pipeline is transcribe→resolve→DB, only the
transcribe box matters, so `paddle_ab.py` swaps in PaddleOCR (PP-OCR models via
ONNX Runtime / RapidOCR — portable CPU, paddlepaddle's own CPU wheels SIGILL on
Modal) and runs both engines' transcripts through the SAME `resolve_member_id`.

| Engine | correct | auto-accept | false-accept | latency (warm) |
|---|---|---|---|---|
| Qwen3-VL (L4) | 30/50 (60%) | 2/50 | 0 | ~25 s |
| PaddleOCR (CPU) | **0/50 (0%)** | 0 | 0 | **~1.4 s** |

PaddleOCR is ~17× faster and free of GPU cost, but read **0/50** on the current
photos — even with detection resolution raised to 1920. Transcript dump shows why:
it reads the **prominent** printed text (product code `R888`, routing `航达B04 /
广州转`) but **misses the small member_id**, which the VLM recovers. Same root cause
as the accuracy gap: **the id is too small/faint in these bad captures** for classic
OCR. KEY IMPLICATION: the framing fix is doubly valuable — a legible, well-framed id
would let a **1.4s CPU OCR** replace the 25s GPU VLM, solving *both* accuracy and
latency (and cost). Not viable on today's photo quality; re-test PaddleOCR on real
high-visibility photos. `modal run paddle_ab.py::ab` reproduces this.

**Qwen latency optimization (2026-07-09, in progress).** `modal_app.py::bench`
times warm transcribe; GPU is env-selectable (`SILKWAY_GPU`), and image/output
caps are env-tunable (`MAX_IMAGE_SIDE`, `MAX_NEW_TOKENS`; both default to original
behaviour). Measured (image_silkway):

| GPU | warm latency |
|---|---|
| L4 (current) | ~25s, variable to 56s |
| A10G + caps | ~14s, stable |
| A100 / L40S | blocked — Modal account needs a payment method |

Cheap wins (image cap, token cap) don't move the needle much; the GPU dominates,
and even A10G is ~14s. Reaching 2–3s needs **vLLM** (transformers `.generate()` is
the slow part; vLLM ~3–5× faster) and likely a premium GPU (A100/L40S). Note: a low
`MAX_NEW_TOKENS` truncates the transcript (risking accuracy), so any shipped setting
must be accuracy-re-verified. Prod endpoint unchanged (still L4, defaults).

**vLLM tried on A10G — no win (2026-07-09).** `vllm_bench.py` serves the same
Qwen3-VL-8B via vLLM (CUDA `-devel` base image for nvcc). Result: warm ~11–14s once
fully warmed (variable 11–108s early), **no better than transformers**. Root cause
in the init logs: the 8B model barely fits the 24GB A10G (only **1.16 GiB** left for
KV cache), forcing `enforce_eager=True` (no CUDA graphs) — and CUDA graphs are where
vLLM's speedup lives. **Conclusion: the A10G is too small for this 8B VLM to go
fast; ~11s is the floor on it either way.** Reaching 2–3s requires a bigger GPU
(A100/L40S: memory headroom → CUDA graphs + KV cache) which needs a **Modal payment
method** (premium GPUs are gated). Realistic but unproven + costs real money
(premium GPU warm 24/7). Meanwhile PaddleOCR is ~1.4s free on CPU, blocked only by
capture quality — so the framing fix remains the far cheaper route to low latency.

## HTTP endpoint (send a photo → get the member_id)

`modal_app.py::web` is a deployed FastAPI service that runs the **whole pipeline
in the cloud**: POST a photo → GPU transcribe → Postgres `resolve_member_id` →
JSON decision back. One call, final answer. It is a separate CPU function (scales
to zero) that forwards image bytes to the existing `QwenOCR` GPU class over
Modal's internal network — the GPU class and its image are unchanged, so the
model deploy and the local entrypoints are unaffected.

**One-time setup** — put the db creds + a bearer token in a Modal secret (the
local entrypoints keep using `.env`; only the endpoint reads this):

```
modal secret create silkway-secrets \
  DB_HOST=... DB_PORT=4444 DB_USER=... DB_PASSWORD=... DB_NAME=... API_TOKEN=<pick-one>
```

`API_TOKEN` is optional: absent → endpoint runs open (fine behind a private
gateway); present → every request needs `Authorization: Bearer <token>`.

**Deploy:** `modal deploy modal_app.py` → prints the URL for `web`.

**Call it:**
```
curl -X POST https://<you>--silkway-ocr-web.modal.run/recognize \
  -H "Authorization: Bearer <token>" \
  -F "file=@label.jpg"
```

Response = the full resolver decision plus the raw transcript:
```json
{"transcript": "...首都波960662号...", "status": "accept", "member_id": "960662",
 "confidence": "high", "source": "marker", "reason": "marker match confirmed in db"}
```
`status` is `accept` (auto) or `manual` (queue, `member_id` pre-filled when we
have a guess, `candidates` listed when ambiguous). `GET /health` is a gpu-free
liveness check; `/docs` serves the OpenAPI UI.

**Deployed & live-validated (2026-07-07)** at
`https://adilet-amangossov--silkway-ocr-web.modal.run` (secret `silkway-secrets`
holds the db creds + `API_TOKEN`). Verified end-to-end through the live URL:
`/health` ok; tokenless `/recognize` → 401; `image_silkway.jpeg` → `accept`
`960662` (source marker, high); `problem_photo.jpg` (no id) → `manual` None — the
never-false-accept invariant holds over HTTP.

**Per-integrator API tokens (`auth.py`, 2026-07-08).** The endpoint accepts a map
of named tokens (`API_TOKENS` JSON in the Modal secret) — each integrator gets its
own, revocable independently; the legacy single `API_TOKEN` still works as consumer
`default`. The matched integrator name is logged with each decision (new `consumer`
column on `ocr_decisions`, additive). `manage_tokens.py add/revoke/list/sync`
manages the map in `.env` and pushes it to the secret. `sync` pushes only the
per-integrator map (not the legacy single `API_TOKEN`, which is now a local-only
convenience for `client.py`), so it always yields a per-integrator-only secret. Live-verified: multiple
integrator tokens all authorize, a random token → 401, and revoking one integrator
left the others (and the legacy token) working; consumer recorded as `default` on a
real call. Comparison is constant-time; open only when nothing is configured (dev).

**Durable decision logging (2026-07-07).** The container filesystem is ephemeral,
so the endpoint writes every decision to an `ocr_decisions` table in the *same*
Postgres (`decision_sink.py:PostgresDecisionSink`; additive `CREATE TABLE IF NOT
EXISTS`, never touches `users`/`cargo_parcels`). One row per request: photo,
platform (optional `/recognize` form field), transcript, the flattened decision
(status/member_id/confidence/source/reason), full decision as JSONB, and
`corrected_id` (NULL now, backfilled from the manual queue later). Logging is
best-effort (never fails the request) and the resolve+insert run in a worker
thread so concurrent requests don't serialize on the db. Live-verified: a
`/recognize` call landed row `id 1` in `ocr_decisions`. This is the sink that
turns real traffic into the labeled per-platform eval set the project still
needs. `SqliteDecisionSink` mirrors it for offline tests.

**Ground-truth backfill + live accuracy (2026-07-07).** `backfill_gt.py` scores
the logged decisions with no GPU. For any photo whose filename carries a parcel id
(`parcel_<id>.jpg`, as `build_db_eval.py` names them), it fills `corrected_id` in
one idempotent set-based UPDATE via the same linkage `eval_platforms` trusts —
`cargo_parcels.parcel_id → user_id → users.member_id` — then reuses
`evaluation.py` to print a per-platform `correct / accept / false_accept` table.
Rows with an unlinkable (arbitrary production) filename stay unscored until a human
backfills them. Live-verified end-to-end: `parcel_2671147.jpg` sent through the
endpoint read `968690` (correct, routed to `manual`/`db_scan` — no marker), the
backfill linked its ground truth and scored it `1/1 correct, 0 false-accept`;
re-running backfilled 0 (idempotent). Run: `python backfill_gt.py`.

**Evidence loop verified end to end (2026-07-07).** After the endpoint, sink, and
client were live, a `backfill_gt.py` run over the accumulated `ocr_decisions`
(rows generated by this session's own testing — the endpoint + integration
dry-runs, *not* real specialist traffic yet) linked ground truth and printed a
per-platform table: `2/3 correct, 0 auto-accept, 0 false-accept` on the 3 rows
with a `parcel_<id>.jpg` filename (4 `image_silkway.jpeg` calls stayed unlinkable,
correctly skipped). 0 auto-accepts is expected — these are old pre-high-visibility
photos where the marker path doesn't fire. So `endpoint → ocr_decisions →
backfill_gt → scored table` is proven wired; pointing genuine high-visibility
traffic (named `parcel_<id>.jpg`) at `/recognize` will yield the real per-platform
accuracy the project still needs.

## Client (`client.py`)

The caller a parcel backend imports, or a person runs from the terminal.
Zero third-party deps (stdlib `urllib` + a hand-rolled multipart encoder), so it
drops into any backend. `recognize(image, platform=None) -> decision` takes a path
or raw bytes; the CLI is `python client.py label.jpg [--platform taobao]`.
Endpoint URL + bearer token come from the environment (`OCR_ENDPOINT_URL`,
`API_TOKEN`; the CLI loads `.env`), so the token stays server-side, never in code.
A non-200 raises `RuntimeError` carrying the server's error body.

Live-verified against the deployed endpoint: CLI and the imported `recognize()`
both returned `accept` `960662` for `image_silkway.jpeg`; a wrong token surfaced
`RuntimeError: endpoint returned HTTP 401 ...`. `_encode_multipart` is pure and
unit-tested (4 tests). `.env.example` documents the two new client vars.

**Backend integration reference (`integration_example.py`).** A sketch to adapt
into the parcel backend (not part of the deployed service). Per parcel: download
the photo → `client.recognize(bytes, filename=f"parcel_<id>.jpg")` → branch on the
decision. `accept` → write the db-confirmed `member_id` to
`cargo_parcels.found_member_id` (dedicated column; never touches `users`);
`manual` → `enqueue_manual()` stub hands it to the specialist queue with the guess
prefilled + any candidates. Naming the upload `parcel_<id>.jpg` makes those calls
auto-scoreable by `backfill_gt.py` later. Writes are OFF by default (dry run);
`--commit` enables the `found_member_id` write. Live dry-run over 2 real parcels
exercised the fetch → recognize → manual-queue path with no db writes.

## FastAPI parcel backend (`backend_app.py` + `manual_queue.py`)

The event-driven version of the integration, as a real service (distinct from the
Modal OCR endpoint it calls). A specialist uploads a photo for a parcel; the
service reads the member_id via `client.recognize` and routes:

- `POST /parcels/{id}/recognize` (multipart photo) → **accept** writes the
  db-confirmed `member_id` to `cargo_parcels.found_member_id`; **manual** inserts
  a pending row into the **`ocr_manual_queue`** Postgres table with the OCR guess
  pre-filled. Returns `{action: accepted|queued, ...}`.
- `GET /manual-queue` → the specialist work list (pending items + candidates).
- `POST /manual-queue/{item_id}/resolve` (form `member_id`) → marks the row
  resolved and writes the verified id to the parcel, closing the loop. A
  double-resolve is a no-op (never overwrites the first specialist's answer).

`manual_queue.py` mirrors the store pattern: `PostgresManualQueue` (dedicated
`ocr_manual_queue` table, additive `CREATE TABLE IF NOT EXISTS`, never touches
`users`/`cargo_parcels`) + `SqliteManualQueue` for offline tests. The queue,
recognizer, and found_member_id writer are injected as FastAPI dependencies, so
routing is unit-tested with stubs via `TestClient` — no Modal, no prod db (11 new
tests). The Postgres `ocr_manual_queue` table was created + verified live.
`fastapi[standard]` added to requirements. Run: `uvicorn backend_app:app --reload`.

**Auth (2026-07-07).** The three data routes require `Authorization: Bearer
<BACKEND_API_TOKEN>` via a `require_auth` dependency; `/health` stays open.
Enforced only when `BACKEND_API_TOKEN` is set (open for local dev — **set it in
any real deployment**); it's the backend's own token, separate from the OCR
endpoint's `API_TOKEN`. Verified live against the real Postgres queue: no/wrong
token → 401, correct token → 200, `/health` open (2 auth tests; 76 total). The
API docs (`/docs`, `/redoc`, `/openapi.json`) are gated too (built-ins disabled,
re-added behind `require_docs_auth`) so the schema isn't public; they accept the
token as a bearer header **or** a `?token=` query param, so `/docs?token=…` still
opens in a browser (Swagger's schema fetch carries the token forward). Open when
no token is configured (dev). Verified live: no token → 401, `?token=` → 200.

**Connection pooling (`db.py`, 2026-07-07).** Every Postgres store used to open a
fresh connection (+ TLS handshake) per query — fine for a one-off script, wasteful
for the long-running services. `db.py` holds a process-wide, lazily-created
`psycopg_pool.ConnectionPool` (`min_size=1`, `max_size` via `DB_POOL_MAX`, default
5) exposed as a `connection()` context manager with the same commit-on-exit
semantics. `PostgresUserIDStore`, `PostgresDecisionSink`, `PostgresManualQueue`,
and the backend's parcel-writer all route through it; the one-off scripts
(`build_db_eval`, `backfill_gt`, `integration_example`) keep their own single
connection. The pool imports psycopg-pool lazily, so the sqlite stubs and unit
tests stay dependency-free (80 tests still pass). Verified live: 6 store queries
served by the pool reused 2 connections (was 6 reconnects); the redeployed OCR
endpoint resolves through the in-container pool. `psycopg-pool` added to
requirements and the Modal `web_image`.

**Specialist review UI (`specialist_ui.html`, 2026-07-07).** A self-contained
page (no external deps, light/dark, mobile-friendly) served open at `GET /` — the
shell is public; the data it loads is behind the token. The specialist enters
their name + access token (persisted in localStorage), the page lists pending
items from `GET /manual-queue`, and each card shows the parcel photo (via the new
`image_url`), the OCR guess pre-filled in an editable field, candidate chips
(click to fill), the reason, and the OCR transcript (collapsible). Confirm →
`POST /manual-queue/{id}/resolve` (with `resolved_by`), then the card drops out.
To feed the photo, the queue gained an `image_url` column (additive: CREATE plus
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, applied to the live table) and
`POST /parcels/{id}/recognize` takes an optional `image_url` form field. Verified
live end to end against the real Postgres queue (seeded → listed with image_url →
deleted); 2 new tests (78 total). Visual treatment: blue-biased cool-slate token
palette (theme-aware via `prefers-color-scheme` + `data-theme` overrides), member
ids / candidate chips / label digits in a monospace face with `tabular-nums`
(the product is about reading digits), accent-highlighted marker, and an SVG
label fallback drawn from the OCR transcript when a photo url is absent OR fails
to load — which also covers the known CDN-purge case (old image urls 404).

## Shipped & live-validated

- **Modal app `silkway-ocr` deployed** — Qwen3-VL-8B, weights in a `modal.Volume`,
  loaded in `@modal.enter()`, `memory=16384` reserved to stop the cold-start
  weight-load OOM (exit 137) from intermittently killing containers.
- **Fuzzy-marker auto-accept** (`extraction.has_warehouse_marker` + `validation.py`
  tier): OCR often mangles the exact `首都波` glyphs (`首都表`/`首部城`), so a looser
  warehouse signal (`库区`/`航达`/`首[都部]`) plus a unique DB-valid `号`-terminated id
  run now auto-accepts. Runs locally in the entrypoint — no Modal redeploy needed.
- **Format-agnostic extraction** (`extraction.py`): `find_member_id_candidates`
  collects every maximal 5–7 digit run; DB uniqueness picks the real id among
  same-length decoys. Marker path (`首都波\s*(\d{5,7})\s*号`) kept on top as the
  highest-confidence signal when present.
- **Masked-digit wildcard tier** (`validation.py` + `extraction.py:wildcard_patterns`):
  single-blank SQL `LIKE` patterns (misread digit in place, or obscured digit
  dropped by OCR) resolved via `store.find_matching(...)`. Never auto-accepts.
  Pays off for **sparse** cases only — the DB is too dense for dense-range
  occlusions, which correctly route to manual.
- **Anti-repetition decoding** (`modal_app.py::transcribe`): `repetition_penalty=1.1`
  + `no_repeat_ngram_size=4` — fixed the degenerate max-token loop on dark images.
- **End-to-end validated on live Postgres** (35,399 users) via `modal_app.py::e2e`:
  all 3 sample photos pass the core invariant — *never auto-accept a label with
  no real id*.
  - `image_silkway.jpeg`: marker `首都波960662号` → accept high (real client).
  - `problem_photo.jpg`: no-id routing sticker → manual None.
  - `hided.jpg`: red-penned digit `96638号` → manual None ("37 db ids within a
    single digit, unrecoverable").
- **Decision logging + eval harness** (`decision_log.py`, `evaluation.py`,
  `modal_app.py::eval_platforms`): append-only JSONL decision log; per-platform
  scoring. Key metric is `false_accept` (auto-accepted **wrong** id) — must stay
  low.
- **Offline ground-truth audit** (`gt_audit.py`): replays `decisions.jsonl`
  against verified ids with no db/gpu, so eval claims are reproducible. Prints the
  shipped table and the marker-free counterfactual. Run: `python gt_audit.py`.
- **49 unit tests passing** (`extraction`, `validation`, `decision_log`,
  `evaluation`); all pure/offline-testable.

## Key findings

- Two of three observed failure modes were **absent or destroyed ids** (a routing
  sticker carrying no id; a red sorting-pen scrawl over a digit) — unrecoverable
  by any OCR, not model-capability gaps. The third works.
- **Operational fix agreed with warehouse (2026-06-26):** specialists now frame
  the member_id with high visibility, which removes the occlusion failure mode for
  go-forward volume.
- **Fine-tuning is premature.** The 5 marker-free false-accept cases were
  inspected (2026-07-03): they are dark/underexposed photos or printed-label-vs-DB
  discrepancies (`903372` case), not legible-digit misreads the model should have
  gotten. Precision comes from DB-as-arbiter, not perfect OCR. The collected eval
  dataset doubles as a fine-tune set if it ever proves needed.

## Where we are — the one open blocker

The pipeline has now been run against **78 real DB photos** (recent + a cross-date
Sept-2025..Jul-2026 sample; `decisions.jsonl` holds the full log). Those are
**old, pre-high-visibility** courier photos — mostly dark, sticker id incidental.
The remaining gap: `eval_platforms` has **never been run on Taobao / Pinduoduo /
Poizon photos under the new high-visibility framing** agreed with the warehouse.
`eval_manifest.json` still holds only the 3 seed samples.

Note: the image CDN purges old photos (most pre-2026 URLs 404), so eval sets can
only be built from recent parcels going forward.

## Next actions

1. Collect real per-platform photos (Taobao / Pinduoduo / Poizon), high-visibility
   framing, and drop them in `eval_samples/`.
2. Add them to `eval_manifest.json` with ground-truth member_ids.
3. Run `modal run modal_app.py::eval_platforms` and read the per-platform table.
4. Only if a platform shows real **legible-digit misreads**: try prompt / crop /
   preprocess first (cheap, reversible); fine-tune only if those fall short.

## Still deferred

- Marker variation across carriers (only `首都波` observed so far).
- Near-miss / fuzzy resolution beyond the single-digit wildcard tier.
- Fine-tuning (gated on eval data showing legible-digit misreads).
