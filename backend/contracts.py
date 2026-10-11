"""
Kontrak antar-anggota (Project Plan V2, bagian 2).

Data types yang dipakai bersama oleh pipeline, plus stub untuk fungsi Anggota A
(redact, acquire, retrieve_context, check_impact) selama modul aslinya belum ada.
backend/graph.py memakai modul asli Anggota A kalau sudah tersedia, dan jatuh ke stub di sini kalau belum.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


# --- Anggota A ---

@dataclass
class RedactedLog:
    text: str
    entity_count: int = 0


@dataclass
class ContextBundle:
    files: dict[str, str] = field(default_factory=dict)  # path -> source
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)


# --- Anggota B ---

@dataclass
class ValidationResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    # True when the patch broke a hard rule (diff budget, protected path) and should go to a human
    escalate: bool = False
    # Per layer: {"ast": "passed", "ruff": "failed", "bandit": "skipped: not installed", ...}
    layers: dict[str, str] = field(default_factory=dict)


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class

    passed: bool
    output: str = ""
    skipped: bool = False          # sandbox not available, nothing was run
    mode: str = ""                 # "docker", "local" or "off"
    reproduced: bool | None = None  # did the tests fail BEFORE the patch?
    before_output: str = ""
    command: str = ""


# --- Stubs for Anggota A's functions (replaced by the real modules once they exist) ---

def redact_stub(raw_log: str) -> RedactedLog:
    return RedactedLog(text=raw_log, entity_count=0)


def acquire_stub(fingerprint: str) -> bool:
    return True


def retrieve_context_stub(file_path: str) -> ContextBundle:
    return ContextBundle()


def check_impact_stub(old_src: str, new_src: str) -> list[str]:
    return []


def fingerprint_stub(repo: str, file_path: str, error_log: str) -> str:
    """hash(repo + file + exception type + last error line with numbers/ids removed). Anggota A owns the real one."""
    lines = [line.strip() for line in (error_log or "").strip().splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    exception_type = last.split(":", 1)[0] if ":" in last else last
    normalized = re.sub(r"0x[0-9a-fA-F]+|\d+", "N", last)
    raw = f"{repo}|{file_path}|{exception_type}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
