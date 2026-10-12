"""
Simple storage for pipeline runs so the Streamlit dashboard can read them via GET /runs.
Runs are kept in memory and saved to data/runs.json so they survive a backend restart.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone

STORE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "runs.json")
MAX_RUNS = 50

_lock = threading.Lock()
_runs: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Statuses after which a run never changes again
FINAL_STATUSES = ("done", "failed", "duplicate", "escalated")


def _save() -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    # Write to a temp file first so a crash mid-write never leaves a broken runs.json
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(list(_runs.values()), f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, STORE_PATH)


def _load() -> None:
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, "r", encoding="utf-8") as f:
                for run in json.load(f):
                    # A run that was still in progress when the backend stopped will never finish
                    if run.get("status") not in FINAL_STATUSES:
                        run["status"] = "failed"
                        run["error"] = run.get("error") or "Backend restarted before this run finished."
                    _runs[run["id"]] = run
        except (json.JSONDecodeError, KeyError):
            pass


def create_run(payload: dict) -> dict:
    run = {
        "id": uuid.uuid4().hex[:8],
        "received_at": _now(),
        "updated_at": _now(),
        # received -> redacting -> researching -> engineering -> validating -> testing -> creating_pr -> done
        # (engineering/validating/testing repeat up to 3 times), or -> duplicate / escalating -> escalated / failed
        "status": "received",
        "payload": payload,  # webhook metadata (repo, file_path, branch, commit, run_url), without the raw log
        "error_log": None,   # redacted error log
        "redaction_count": None,
        "fingerprint": None,
        "occurrences": None,
        "analysis": None,
        "original_code": None,  # file content before the fix, used for the diff in the dashboard
        "iteration": 0,
        "engineer": None,  # latest {"summary", "fixed_code", "language", "valid", "error"}
        "sast": None,      # {"passed", "reasons", "escalate", "layers"}
        "tests": None,
        "attempts": [],    # every patch tried: {"iteration", "stage", "summary", "diff", "feedback"}     # {"passed", "output", "skipped", "mode", "reproduced", "before_output", "command"}
        "pr": None,        # {"pr_url", "pr_number", "branch", "base_branch", "reused", "draft", "labels"} or {"error"}
        "issue": None,     # {"issue_url", "issue_number", "slack_notified"} when escalated
        "error": None,
    }
    with _lock:
        _runs[run["id"]] = run
        # Keep only the newest MAX_RUNS runs
        if len(_runs) > MAX_RUNS:
            for old_id in sorted(_runs, key=lambda k: _runs[k]["received_at"])[:-MAX_RUNS]:
                del _runs[old_id]
        _save()
    return run


def update_run(run_id: str, **fields) -> None:
    with _lock:
        if run_id in _runs:
            _runs[run_id].update(fields, updated_at=_now())
            _save()


def get_run(run_id: str) -> dict | None:
    with _lock:
        return _runs.get(run_id)


def list_runs() -> list[dict]:
    """Newest first."""
    with _lock:
        return sorted(_runs.values(), key=lambda r: r["received_at"], reverse=True)


_load()
