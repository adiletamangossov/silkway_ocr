import json
import os
from datetime import datetime, timezone

# append-only JSONL log of every resolution decision. two payoffs: real runs
# accumulate a labeled dataset (the exact input any future fine-tuning or
# accuracy audit needs), and we get to see the true per-platform error
# distribution instead of guessing it. the file path comes from an env var so
# nothing about the deployment is hardcoded here.
DEFAULT_LOG_PATH = "decisions.jsonl"


def log_path() -> str:
    return os.environ.get("DECISION_LOG_PATH", DEFAULT_LOG_PATH)


def log_decision(
    photo: str,
    transcript: str,
    decision: dict,
    platform: str | None = None,
    corrected_id: str | None = None,
    path: str | None = None,
) -> dict:
    # write one json object per line. corrected_id is the human-verified answer:
    # usually unknown at decision time (None) and backfilled later from the manual
    # queue, but filled directly when we already hold ground truth (eval runs).
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "photo": os.path.basename(str(photo)),
        "platform": platform,
        "transcript": transcript,
        "decision": decision,
        "corrected_id": corrected_id,
    }
    target = path or log_path()
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_decisions(path: str | None = None) -> list[dict]:
    # load every logged record back, in order. used to build the labeled dataset
    # and to audit accuracy after the fact. a missing log file is just no data.
    target = path or log_path()
    if not os.path.exists(target):
        return []
    records: list[dict] = []
    with open(target, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
