"""
Rate limiting & circuit breaker (Sub-Task 11, Anggota B).

- PR quota: at most AUTOHEAL_MAX_PRS_PER_HOUR PRs per repo per hour.
- Cooldown: after a PR for an error fingerprint, the same fingerprint gets no new PR for
  AUTOHEAL_FINGERPRINT_COOLDOWN seconds (repeats become comments on the existing PR).
- Circuit breaker: after AUTOHEAL_BREAKER_THRESHOLD failed fixes in a row on a repo, the agent stops
  working on that repo until AUTOHEAL_BREAKER_RESET seconds have passed or it is reset by hand.
- GitHub API: read X-RateLimit-Remaining (through PyGithub) and back off exponentially near the limit.

State lives in memory and is saved to data/rate_limiter.json so a restart doesn't reset the quota.
Like run_store, this is single-process only.
"""
from __future__ import annotations

import json
import os
import threading
import time

STORE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "rate_limiter.json")

MAX_PRS_PER_HOUR = int(os.environ.get("AUTOHEAL_MAX_PRS_PER_HOUR", "3"))
FINGERPRINT_COOLDOWN = int(os.environ.get("AUTOHEAL_FINGERPRINT_COOLDOWN", str(30 * 60)))
BREAKER_THRESHOLD = int(os.environ.get("AUTOHEAL_BREAKER_THRESHOLD", "3"))
BREAKER_RESET = int(os.environ.get("AUTOHEAL_BREAKER_RESET", str(60 * 60)))
# Back off when fewer than this many GitHub API calls are left
GITHUB_MIN_REMAINING = int(os.environ.get("AUTOHEAL_GITHUB_MIN_REMAINING", "100"))
MAX_BACKOFF = 60

_lock = threading.Lock()
_state: dict = {}
_sleep = time.sleep  # replaced in tests
_now = time.time


def _empty_state() -> dict:
    # pr_times: repo -> [timestamps]; fingerprints: fp -> {"last_pr", "pr_number", "occurrences"}
    # failures: repo -> consecutive failed fixes; breaker_opened: repo -> timestamp
    return {"pr_times": {}, "fingerprints": {}, "failures": {}, "breaker_opened": {}}


def _load() -> None:
    global _state
    _state = _empty_state()
    if os.path.exists(STORE_PATH):
        try:
            with open(STORE_PATH, "r", encoding="utf-8") as f:
                _state.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass


def _save() -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_state, f, indent=2)
    os.replace(tmp_path, STORE_PATH)


def reset() -> None:
    """Forget everything (used by tests)."""
    with _lock:
        _state.clear()
        _state.update(_empty_state())


# ---------------------------------------------------------------------------
# PR quota + fingerprint cooldown
# ---------------------------------------------------------------------------

def in_cooldown(fingerprint: str) -> bool:
    with _lock:
        entry = _state["fingerprints"].get(fingerprint)
        return bool(entry and entry.get("last_pr") and _now() - entry["last_pr"] < FINGERPRINT_COOLDOWN)


def check_pr_allowed(repo: str, fingerprint: str, reserve: bool = True) -> tuple[bool, str]:
    """
    Return (allowed, reason). With reserve=True an allowed call also books the PR slot right away,
    so two runs that check at the same moment can't both get the last slot.
    """
    with _lock:
        now = _now()
        if _breaker_open_locked(repo, now):
            return False, f"circuit breaker is open for {repo} after {BREAKER_THRESHOLD} failed fixes in a row"
        entry = _state["fingerprints"].get(fingerprint, {})
        if entry.get("last_pr") and now - entry["last_pr"] < FINGERPRINT_COOLDOWN:
            wait = int(FINGERPRINT_COOLDOWN - (now - entry["last_pr"]))
            return False, f"this error already got a PR recently (cooldown, {wait}s left)"
        recent = [t for t in _state["pr_times"].get(repo, []) if now - t < 3600]
        if len(recent) >= MAX_PRS_PER_HOUR:
            return False, f"PR quota reached for {repo} ({MAX_PRS_PER_HOUR} per hour)"
        if reserve:
            _state["pr_times"][repo] = recent + [now]
            _state["fingerprints"][fingerprint] = {**entry, "last_pr": now}
            _save()
        return True, "ok"


def can_open_pr(repo: str, fingerprint: str) -> bool:
    """Contract function (Project Plan V2). Books the slot when it returns True."""
    return check_pr_allowed(repo, fingerprint)[0]


def release_pr_slot(repo: str, fingerprint: str) -> None:
    """Give the slot back when the PR could not be opened after all."""
    with _lock:
        entry = _state["fingerprints"].get(fingerprint, {})
        booked = entry.pop("last_pr", None)
        times = _state["pr_times"].get(repo, [])
        if booked in times:
            times.remove(booked)
        _save()


def remember_pr(fingerprint: str, pr_number: int) -> None:
    with _lock:
        _state["fingerprints"].setdefault(fingerprint, {})["pr_number"] = pr_number
        _save()


def pr_for_fingerprint(fingerprint: str) -> int | None:
    with _lock:
        return _state["fingerprints"].get(fingerprint, {}).get("pr_number")


def record_occurrence(fingerprint: str) -> int:
    """Count how many times this error was reported. Returns the new count."""
    with _lock:
        entry = _state["fingerprints"].setdefault(fingerprint, {})
        entry["occurrences"] = entry.get("occurrences", 0) + 1
        _save()
        return entry["occurrences"]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

def _breaker_open_locked(repo: str, now: float) -> bool:
    opened = _state["breaker_opened"].get(repo)
    if not opened:
        return False
    if now - opened >= BREAKER_RESET:
        # Half-open: give the agent one new chance
        _state["breaker_opened"].pop(repo, None)
        _state["failures"][repo] = BREAKER_THRESHOLD - 1
        _save()
        return False
    return True


def breaker_open(repo: str) -> bool:
    with _lock:
        return _breaker_open_locked(repo, _now())


def record_fix_result(repo: str, success: bool) -> bool:
    """Record a finished fix attempt. Returns True if this failure just opened the breaker."""
    with _lock:
        if success:
            _state["failures"][repo] = 0
            _state["breaker_opened"].pop(repo, None)
            _save()
            return False
        count = _state["failures"].get(repo, 0) + 1
        _state["failures"][repo] = count
        just_opened = count >= BREAKER_THRESHOLD and repo not in _state["breaker_opened"]
        if just_opened:
            _state["breaker_opened"][repo] = _now()
        _save()
        return just_opened


def reset_breaker(repo: str) -> None:
    with _lock:
        _state["failures"][repo] = 0
        _state["breaker_opened"].pop(repo, None)
        _save()


def status() -> dict:
    """Snapshot for the API / dashboard."""
    with _lock:
        now = _now()
        return {
            "limits": {"max_prs_per_hour": MAX_PRS_PER_HOUR, "fingerprint_cooldown": FINGERPRINT_COOLDOWN,
                       "breaker_threshold": BREAKER_THRESHOLD, "breaker_reset": BREAKER_RESET},
            "prs_last_hour": {r: len([t for t in ts if now - t < 3600]) for r, ts in _state["pr_times"].items()},
            "consecutive_failures": dict(_state["failures"]),
            "breaker_open": sorted(_state["breaker_opened"]),
        }


# ---------------------------------------------------------------------------
# GitHub API rate limit
# ---------------------------------------------------------------------------

def backoff_seconds(remaining: int, attempt: int = 0) -> float:
    """Exponential backoff that grows as the remaining quota shrinks below GITHUB_MIN_REMAINING."""
    if remaining >= GITHUB_MIN_REMAINING:
        return 0.0
    # remaining 99 -> 1s, 50 -> 2s, 25 -> 4s ... capped at MAX_BACKOFF
    steps = 0
    level = GITHUB_MIN_REMAINING
    while remaining < level and level > 1:
        level //= 2
        steps += 1
    return float(min(MAX_BACKOFF, 2 ** (steps - 1 + attempt)))


def throttle_github(gh) -> float:
    """
    Read X-RateLimit-Remaining (PyGithub keeps it from the last response) and sleep if we are close
    to the limit. Returns the number of seconds slept.
    """
    try:
        remaining, _limit = gh.rate_limiting
    except Exception:
        return 0.0
    if not isinstance(remaining, int) or remaining < 0:
        return 0.0
    delay = backoff_seconds(remaining)
    if remaining == 0:
        # Out of calls: wait until GitHub resets the quota (X-RateLimit-Reset), capped
        try:
            delay = float(min(MAX_BACKOFF, max(1, gh.rate_limiting_resettime - _now())))
        except Exception:
            delay = float(MAX_BACKOFF)
    if delay > 0:
        print(f"⏳ [RateLimit] Only {remaining} GitHub API calls left, waiting {delay:.0f}s")
        _sleep(delay)
    return delay


_load()
