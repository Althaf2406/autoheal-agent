"""
Modul Deduplikasi Error & Distributed Lock (Sub-Task 10, Anggota A).

1. Menghitung fingerprint unik dan deterministik dari error log:
   hash(repo + file_path + exception_type + normalized_error)
   dengan menormalisasi timestamp, memory addresses, UUIDs, dan angka dinamis.

2. Mengelola distributed lock dengan TTL (Time-To-Live):
   - Mendukung Redis jika REDIS_URL dikonfigurasi.
   - Fallback otomatis ke thread-safe in-memory store dengan TTL expiration.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any

# In-memory lock store: fingerprint -> expires_at (float timestamp)
_lock = threading.Lock()
_in_memory_locks: dict[str, float] = {}


def normalize_error_log(error_log: str) -> str:
    """
    Normalizes an error log by stripping out dynamic values:
    - Timestamps (ISO8601, common formats)
    - Memory addresses (0x...)
    - UUIDs
    - Traceback line numbers (File "...", line 123, in ...)
    - Process/Thread IDs and numeric counters
    """
    if not error_log:
        return ""

    text = error_log.strip()

    # 1. Normalize ISO-8601 and common datetime strings
    text = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
        "<TIMESTAMP>",
        text,
    )
    text = re.sub(r"\b\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2}\b", "<TIMESTAMP>", text)

    # 2. Normalize memory addresses: 0x7fff5fbff8a0
    text = re.sub(r"\b0x[0-9a-fA-F]+\b", "<HEX_ADDR>", text)

    # 3. Normalize UUIDs: 123e4567-e89b-12d3-a456-426614174000
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "<UUID>",
        text,
    )

    # 4. Normalize Python traceback line numbers
    text = re.sub(r'(File "[^"]+", line )\d+(, in )', r"\g<1>N\g<2>", text)

    # 5. Normalize dynamic process/thread IDs and ports
    text = re.sub(r"\b(?:pid|process|thread|port)[:= ]+\d+\b", "<PROCESS_INFO>", text, flags=re.IGNORECASE)

    # 6. Normalize general standalone numbers / IDs
    text = re.sub(r"\b\d+\b", "N", text)

    # 7. Collapse excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_exception_type(error_log: str) -> str:
    """Extracts exception type from the last traceback line if available."""
    lines = [line.strip() for line in (error_log or "").strip().splitlines() if line.strip()]
    if not lines:
        return "UnknownError"
    last_line = lines[-1]
    if ":" in last_line:
        exc_candidate = last_line.split(":", 1)[0].strip()
        # Verify it resembles an exception identifier (e.g. KeyError, ValueError)
        if re.match(r"^[A-Za-z0-9_.]+(?:Error|Exception|Warning|Fault|Interrupt)?$", exc_candidate):
            return exc_candidate
    return last_line


def fingerprint(repo: str, file_path: str, error_log: str) -> str:
    """
    Computes a deterministic 16-character fingerprint for an error:
    hash(repo + file_path + exception_type + normalized_error)[:16]
    """
    norm_log = normalize_error_log(error_log)
    exc_type = extract_exception_type(error_log)
    raw = f"{repo or ''}|{file_path or ''}|{exc_type}|{norm_log}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _get_redis_client():
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis
        return redis.from_url(redis_url)
    except Exception:
        return None


def acquire(fingerprint: str, ttl_seconds: int = 300) -> bool:
    """
    Acquires a distributed lock for the given fingerprint.
    Returns True if lock was acquired, False if already held.
    """
    if not fingerprint:
        return False

    r = _get_redis_client()
    if r:
        try:
            res = r.set(f"autoheal:lock:{fingerprint}", "1", nx=True, ex=ttl_seconds)
            return bool(res)
        except Exception:
            pass  # Fall back to in-memory lock

    now = time.time()
    with _lock:
        expires_at = _in_memory_locks.get(fingerprint)
        if expires_at is not None and expires_at > now:
            return False  # Still locked
        _in_memory_locks[fingerprint] = now + ttl_seconds
        return True


def release(fingerprint: str) -> None:
    """Releases the lock for the given fingerprint."""
    if not fingerprint:
        return

    r = _get_redis_client()
    if r:
        try:
            r.delete(f"autoheal:lock:{fingerprint}")
        except Exception:
            pass

    with _lock:
        _in_memory_locks.pop(fingerprint, None)


def is_locked(fingerprint: str) -> bool:
    """Checks whether the fingerprint is currently locked."""
    if not fingerprint:
        return False

    r = _get_redis_client()
    if r:
        try:
            return bool(r.exists(f"autoheal:lock:{fingerprint}"))
        except Exception:
            pass

    now = time.time()
    with _lock:
        expires_at = _in_memory_locks.get(fingerprint)
        if expires_at is not None and expires_at > now:
            return True
        elif expires_at is not None and expires_at <= now:
            _in_memory_locks.pop(fingerprint, None)
        return False


def reset() -> None:
    """Clears all locks. Useful for test isolation."""
    r = _get_redis_client()
    if r:
        try:
            keys = r.keys("autoheal:lock:*")
            if keys:
                r.delete(*keys)
        except Exception:
            pass

    with _lock:
        _in_memory_locks.clear()
