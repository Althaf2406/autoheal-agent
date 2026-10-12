import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import dedup


@pytest.fixture(autouse=True)
def clean_locks():
    dedup.reset()
    yield
    dedup.reset()


def test_fingerprint_identical_normalized():
    error_1 = """Traceback (most recent call last):
  File "/app/etl/pipeline.py", line 42, in process_batch
    df["revenue"] = df["price"] * df["quantity"]
KeyError: 'price' [pid: 1042 at 0x7fff5fbff8a0 on 2026-10-12T16:20:00Z]
"""

    error_2 = """Traceback (most recent call last):
  File "/app/etl/pipeline.py", line 88, in process_batch
    df["revenue"] = df["price"] * df["quantity"]
KeyError: 'price' [pid: 9999 at 0x7aaa11112222 on 2026-10-12T18:45:12Z]
"""

    fp1 = dedup.fingerprint("owner/repo", "etl/pipeline.py", error_1)
    fp2 = dedup.fingerprint("owner/repo", "etl/pipeline.py", error_2)

    assert fp1 == fp2
    assert len(fp1) == 16


def test_fingerprint_different_exceptions():
    error_1 = "KeyError: 'price'"
    error_2 = "ValueError: 'invalid literal for int()'"

    fp1 = dedup.fingerprint("owner/repo", "etl/pipeline.py", error_1)
    fp2 = dedup.fingerprint("owner/repo", "etl/pipeline.py", error_2)

    assert fp1 != fp2


def test_fingerprint_different_files_or_repos():
    err = "KeyError: 'price'"

    fp_repo1 = dedup.fingerprint("owner/repo1", "etl/pipeline.py", err)
    fp_repo2 = dedup.fingerprint("owner/repo2", "etl/pipeline.py", err)
    fp_file2 = dedup.fingerprint("owner/repo1", "etl/other.py", err)

    assert fp_repo1 != fp_repo2
    assert fp_repo1 != fp_file2


def test_fingerprint_handles_empty():
    fp = dedup.fingerprint("", "", "")
    assert isinstance(fp, str)
    assert len(fp) == 16


def test_acquire_and_duplicate():
    fp = "test-fingerprint-1"
    assert dedup.acquire(fp) is True
    # Duplicate attempt should be rejected
    assert dedup.acquire(fp) is False


def test_release_allows_reacquire():
    fp = "test-fingerprint-2"
    assert dedup.acquire(fp) is True
    assert dedup.is_locked(fp) is True

    dedup.release(fp)
    assert dedup.is_locked(fp) is False
    assert dedup.acquire(fp) is True


def test_lock_ttl_expiration():
    fp = "test-fingerprint-ttl"
    # Acquire with 0.1s TTL
    assert dedup.acquire(fp, ttl_seconds=0.1) is True
    assert dedup.acquire(fp) is False

    time.sleep(0.15)

    # Lock expired, should be acquirable again
    assert dedup.is_locked(fp) is False
    assert dedup.acquire(fp) is True


def test_concurrent_acquire():
    fp = "test-concurrent-lock"
    results = []

    def try_lock():
        return dedup.acquire(fp, ttl_seconds=5)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(try_lock) for _ in range(10)]
        results = [f.result() for f in futures]

    # Exactly 1 thread must acquire the lock, 9 must fail
    assert results.count(True) == 1
    assert results.count(False) == 9
