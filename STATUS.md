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
