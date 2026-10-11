"""Rate limiting, cooldown, circuit breaker and GitHub backoff (Sub-Task 11)."""
import threading
from types import SimpleNamespace

import pytest

from backend import rate_limiter as rl


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(rl, "_now", lambda: now["t"])
    return now


def test_quota_per_repo_per_hour(clock):
    assert [rl.can_open_pr("me/demo", f"fp{i}") for i in range(4)] == [True, True, True, False]
    assert "quota" in rl.check_pr_allowed("me/demo", "fp9")[1]
    assert rl.can_open_pr("me/other", "fp9")  # other repos have their own quota
    clock["t"] += 3601
    assert rl.can_open_pr("me/demo", "fp10")


def test_fingerprint_cooldown(clock):
    assert rl.can_open_pr("me/demo", "fp")
    assert rl.in_cooldown("fp")
    allowed, reason = rl.check_pr_allowed("me/demo", "fp")
    assert not allowed and "cooldown" in reason
    clock["t"] += rl.FINGERPRINT_COOLDOWN
    assert not rl.in_cooldown("fp") and rl.can_open_pr("me/demo", "fp")


def test_check_without_reserve_does_not_book():
    assert rl.check_pr_allowed("me/demo", "fp", reserve=False)[0]
    assert not rl.in_cooldown("fp")


def test_release_gives_slot_back():
    for i in range(3):
        rl.can_open_pr("me/demo", f"fp{i}")
    rl.release_pr_slot("me/demo", "fp2")
    assert rl.can_open_pr("me/demo", "fp2")


def test_reservation_is_atomic_under_concurrency():
    results = []
    threads = [threading.Thread(target=lambda: results.append(rl.can_open_pr("me/demo", "same-fp"))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_occurrences_and_pr_memory():
    assert [rl.record_occurrence("fp") for _ in range(3)] == [1, 2, 3]
    rl.remember_pr("fp", 42)
    assert rl.pr_for_fingerprint("fp") == 42 and rl.pr_for_fingerprint("other") is None


def test_circuit_breaker_opens_after_threshold(clock):
    assert [rl.record_fix_result("me/demo", False) for _ in range(3)] == [False, False, True]
    assert rl.breaker_open("me/demo")
    assert rl.record_fix_result("me/demo", False) is False  # already open, not "just opened" again
    allowed, reason = rl.check_pr_allowed("me/demo", "fp")
    assert not allowed and "circuit breaker" in reason
    assert rl.status()["breaker_open"] == ["me/demo"]


def test_success_resets_failures():
    rl.record_fix_result("me/demo", False)
    rl.record_fix_result("me/demo", False)
    rl.record_fix_result("me/demo", True)
    assert rl.record_fix_result("me/demo", False) is False
    assert rl.status()["consecutive_failures"]["me/demo"] == 1


def test_breaker_half_opens_after_timeout(clock):
    for _ in range(3):
        rl.record_fix_result("me/demo", False)
    clock["t"] += rl.BREAKER_RESET
    assert not rl.breaker_open("me/demo")
    # One more failure in the half-open state opens it again right away
    assert rl.record_fix_result("me/demo", False) is True


def test_manual_reset():
    for _ in range(3):
        rl.record_fix_result("me/demo", False)
    rl.reset_breaker("me/demo")
    assert not rl.breaker_open("me/demo")


def test_state_survives_restart():
    rl.can_open_pr("me/demo", "fp")
    rl.record_fix_result("me/demo", False)
    rl._state.clear()
    rl._load()
    assert rl.in_cooldown("fp") and rl.status()["consecutive_failures"]["me/demo"] == 1


@pytest.mark.parametrize("remaining, expected", [(5000, 0), (100, 0), (99, 1), (49, 2), (24, 4), (1, 32)])
def test_backoff_grows_as_quota_shrinks(remaining, expected):
    assert rl.backoff_seconds(remaining) == expected


def test_throttle_reads_rate_limit_and_sleeps(monkeypatch, clock):
    slept = []
    monkeypatch.setattr(rl, "_sleep", slept.append)
    assert rl.throttle_github(SimpleNamespace(rate_limiting=(4000, 5000))) == 0 and slept == []
    assert rl.throttle_github(SimpleNamespace(rate_limiting=(40, 5000))) == 2 and slept == [2]
    # Out of calls: wait until the reset time, capped at MAX_BACKOFF
    gh = SimpleNamespace(rate_limiting=(0, 5000), rate_limiting_resettime=clock["t"] + 30)
    assert rl.throttle_github(gh) == 30
    gh = SimpleNamespace(rate_limiting=(0, 5000), rate_limiting_resettime=clock["t"] + 3000)
    assert rl.throttle_github(gh) == rl.MAX_BACKOFF


def test_throttle_ignores_unknown_rate_limit():
    class Broken:
        @property
        def rate_limiting(self):
            raise RuntimeError("no network")
    assert rl.throttle_github(Broken()) == 0
