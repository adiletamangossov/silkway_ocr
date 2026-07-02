# Per-platform eval samples

Drop real label photos here and label them in `../eval_manifest.json`, then run:

```
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m modal run modal_app.py::eval_platforms
```

The harness reads each entry, runs the full pipeline (transcribe -> resolve)
against the live database, scores it against the ground-truth `member_id`, and
prints accuracy broken down by platform. Every result is also appended to the
decision log, so the eval set doubles as labeled training data.

## Adding samples

Each manifest entry is one line:

```json
{"file": "eval_samples/taobao_01.jpg", "platform": "taobao", "member_id": "960662"}
```

- `file` — path relative to the manifest (so files in this folder are
  `eval_samples/<name>`).
- `platform` — origin marketplace: `taobao`, `pinduoduo`, `poizon`, ...
  This is the column we want to compare; collect a handful per platform.
- `member_id` — the true client id printed on the label, or `null` if the label
  carries no client id (or the id is physically unrecoverable). `null` means the
  correct outcome is "manual, nothing prefilled" — i.e. we must NOT auto-accept.

## What the numbers mean

- **correct** — landed on the right id (or correctly resolved to nothing).
- **accept** — auto-accepted without a human.
- **false_accept** — auto-accepted the WRONG id. This must stay at zero; it is
  the only outcome that ships a parcel to the wrong client.

Goal of the exercise: see whether any one platform's label format drives real
misreads on *legible* digits. If it does, fix it with prompt/crop/preprocess
first; fine-tuning is only worth it if those fall short.
