# SilkWay OCR

Read a client's **member_id** off a courier-sticker photo and file it automatically,
so warehouse specialists stop keying it by hand.

SilkWay forwards China-marketplace parcels to clients. Each parcel carries a courier
sticker whose receiver-address line embeds the client's member_id (the digits the
client typed into the address field, e.g. `…库区首都波960662号` → `960662`). This
project reads that id from a phone photo.

## How it works

```
 photo ─▶ OCR service (Modal GPU) ─▶ transcript ─▶ resolver ─▶ Postgres (the arbiter)
                                                        │
                        accept (id confirmed in db) ────┤────▶ found_member_id written
                        manual (uncertain / no id)  ────┘────▶ manual queue ─▶ specialist confirms
```

Two ideas carry the whole design:

1. **Transcribe, then parse deterministically.** A vision-language model
   (Qwen3-VL-8B, self-hosted on a Modal GPU) transcribes *all* text on the label.
   Plain Python then finds the id — never the model "deciding" the answer.
2. **The database is the confidence signal, not the model.** VLMs have no reliable
   confidence and can hallucinate a digit into a well-formed number. Every real
   member_id exists in Postgres, so a DB match is what separates a trustworthy read
   from a plausible-looking guess. The one rule that must always hold: **never
   auto-accept a wrong id** — a wrong read is routed to a human, never shipped.

## Repository layout

**Resolution core** (pure, no I/O — the logic that turns a transcript into a decision)
| File | Purpose |
|---|---|
| `extraction.py` | Marker-anchored + fallback digit-run parsing of a transcript. |
| `validation.py` | `resolve_member_id(transcript, store)` — the tiered decision (marker → db-scan → wildcard); `PostgresUserIDStore` + a SQLite stub. |
| `evaluation.py` | Scoring: `correct` / `accepted` / `false_accept`, aggregated per platform. |

**OCR service** (the model, on Modal)
| File | Purpose |
|---|---|
| `modal_app.py` | The Qwen3-VL GPU class, the `/recognize` HTTP endpoint, and eval entrypoints. |
| `client.py` | Zero-dependency Python client + CLI for the endpoint. |

**Parcel backend** (FastAPI — the specialist-facing service)
| File | Purpose |
|---|---|
| `backend_app.py` | Routes: recognize a parcel, list/resolve the manual queue, serve the UI, gated docs. |
| `manual_queue.py` | The `ocr_manual_queue` Postgres table (+ SQLite stub). |
| `specialist_ui.html` | Self-contained review page served at `/`. |

**Infrastructure & evidence**
| File | Purpose |
|---|---|
| `db.py` | Shared psycopg connection pool for the long-running services. |
| `decision_sink.py` | Durable `ocr_decisions` log written by the endpoint. |
| `backfill_gt.py` | Backfill ground truth into `ocr_decisions` and print a per-platform accuracy table. |
| `build_db_eval.py` | Build a labeled eval set of real photos straight from the production DB. |
| `analyze_misses.py` | Bucket eval misses (id absent / misread / resolver-missed) — separates capture problems from model/logic problems. |
| `framing_split.py` | Before/after: split an eval set by whether the id was legibly captured, to quantify the framing lever. |
| `decision_log.py`, `gt_audit.py`, `evaluation.py` | File-based decision log + offline accuracy audit. |
| `integration_example.py` | Batch reference for wiring a backend to the endpoint. |

Tests live in `tests/` (80 tests, all offline — every DB path has a SQLite stub).

## Setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows; use bin/activate on POSIX
pip install -r requirements.txt
cp .env.example .env                                # then fill in the values
```

`.env` (never committed) holds the Postgres credentials plus the client-side
endpoint config. See `.env.example` for the full list:

- `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` — the client database.
- `OCR_ENDPOINT_URL` / `API_TOKEN` — used by `client.py` to call the deployed endpoint.
- `BACKEND_API_TOKEN` — auth for the FastAPI backend.
- `DB_POOL_MAX` (optional) — pool size cap per process (default 5).

## The OCR service

Self-hosted Qwen3-VL-8B on a Modal L4 GPU: weights live in a `modal.Volume`, loaded
once per container. DB credentials and the endpoint token come from a Modal secret,
never code:

```bash
modal secret create silkway-secrets \
  DB_HOST=... DB_PORT=4444 DB_USER=... DB_PASSWORD=... DB_NAME=... API_TOKEN=<pick-one>
modal deploy modal_app.py            # prints the web URL
```

Call it — one photo in, the final decision out:

```bash
curl -X POST https://<workspace>--silkway-ocr-web.modal.run/recognize \
  -H "Authorization: Bearer <API_TOKEN>" -F "file=@label.jpg"
```

```json
{"transcript": "...首都波960662号...", "status": "accept", "member_id": "960662",
 "confidence": "high", "source": "marker", "reason": "marker match confirmed in db"}
```

`status` is `accept` (auto) or `manual` (queue; `member_id` pre-filled when there's a
guess, `candidates` listed when ambiguous). `API_TOKEN` is enforced only when set.
Every call is logged to the `ocr_decisions` table.

Or from Python (`client.py`, stdlib-only):

```python
from client import recognize
decision = recognize("label.jpg", platform="taobao")   # path or raw bytes
```

## The parcel backend

The specialist-facing FastAPI service (distinct from the Modal endpoint it calls):

```bash
uvicorn backend_app:app --reload      # http://localhost:8000/
```

| Route | Auth | Purpose |
|---|---|---|
| `POST /parcels/{id}/recognize` | 🔒 | Recognize a parcel photo; `accept` writes `found_member_id`, `manual` enqueues it. |
| `GET /manual-queue` | 🔒 | Pending items for the specialist UI. |
| `POST /manual-queue/{id}/resolve` | 🔒 | Record the verified id and write it to the parcel. |
| `GET /` | open | The specialist review page (data behind the token). |
| `GET /health` | open | Liveness. |
| `GET /docs`, `/redoc`, `/openapi.json` | 🔒 | API docs (token via header or `?token=`). |

Auth uses `BACKEND_API_TOKEN`, enforced only when set. The specialist enters their
name + token in the page; on the page each pending parcel shows the photo, the OCR
guess pre-filled, candidate chips, and the transcript — Confirm resolves it.

## The evidence loop

Real traffic becomes a labeled eval set with no manual labeling:

```
/recognize  ─▶  ocr_decisions (logged)  ─▶  backfill_gt.py  ─▶  per-platform accuracy
```

- `python backfill_gt.py` links each decision whose photo is named `parcel_<id>.jpg`
  to its ground truth (`cargo_parcels.parcel_id → user_id → users.member_id`) and
  prints a `correct / accept / false_accept` table. It is idempotent.
- `python build_db_eval.py [N]` builds an N-photo labeled set from the production DB;
  `modal run modal_app.py::eval_platforms --manifest <manifest>` scores the pipeline
  against it.

The key metric is **`false_accept`** — an auto-accepted *wrong* id. It must stay at
zero; a wrong manual prefill is recoverable, a wrong auto-accept misdelivers a parcel.

## Development

```bash
pytest -q          # 80 tests, all offline (SQLite stubs for every DB path)
```

The resolution core (`extraction`, `validation`, `evaluation`) is pure and has no
model, DB, or Modal dependency, so it is fully unit-tested without infrastructure.

See [STATUS.md](STATUS.md) for the current state, real-data eval results, and the
open questions.
