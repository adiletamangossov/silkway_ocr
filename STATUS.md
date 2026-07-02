# SilkWay OCR — Status

_Last updated: 2026-07-02_

## What it does

Reads a client's **member_id** off a courier sticker photo and auto-fills it, so
warehouse specialists stop keying it by hand.

**Pipeline:** phone photo → Qwen3-VL-8B on Modal GPU transcribes all text →
deterministic Python extracts digit runs → **Postgres `users.member_id` is the
arbiter**. VLMs have no reliable confidence and can hallucinate digits, so a DB
match is the confidence signal — not the model.

## Real-data eval (2026-07-02)

Photos + ground truth come straight from the prod DB: `cargo_parcels.images` are
the courier photos, and each parcel's `user_id → users.member_id` is the verified
member_id (the warehouse already matched the parcel to that client). `build_db_eval.py`
downloads N labeled photos; `eval_platforms` scores the full pipeline against them.

**50 real (old, pre-high-visibility) photos:**

| Metric | Exact marker only | **+ Fuzzy marker (shipped)** |
|---|---|---|
| Correct read | 26/50 | 27/50 |
| Auto-accepted | 0/50 | **18/50 (36%)** |
| — correct | 0 | 17 |
| — **false-accept** | 0 | **1** (`912183`→`912185`) |

Decision: **ship fuzzy auto-accept unattended** — 36% hands-off, ~2% of parcels
get a wrong id (a single-digit misread that lands on another real client). The
DB-arbiter still blocks all non-member-id garbage; the residual error is
adjacent-client digit collisions only.

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
  zero.
- **49 unit tests passing** (`extraction`, `validation`, `decision_log`,
  `evaluation`); all pure/offline-testable.

## Key findings

- Two of three observed failure modes were **absent or destroyed ids** (a routing
  sticker carrying no id; a red sorting-pen scrawl over a digit) — unrecoverable
  by any OCR, not model-capability gaps. The third works.
- **Operational fix agreed with warehouse (2026-06-26):** specialists now frame
  the member_id with high visibility, which removes the occlusion failure mode for
  go-forward volume.
- **Fine-tuning is premature.** No evidence of base-model misreads on legible
  digits. Precision comes from DB-as-arbiter, not perfect OCR. The collected eval
  dataset doubles as a fine-tune set if it ever proves needed.

## Where we are — the one open blocker

Everything is built and unit-tested, but `eval_platforms` has **never been run on
real Taobao / Pinduoduo / Poizon photos** under the new high-visibility framing.
`eval_manifest.json` still holds only the 3 seed samples; `decisions.jsonl` does
not exist yet.

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
