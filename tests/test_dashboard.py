"""Render the Streamlit dashboard with a fake backend and check that nothing crashes."""
import os
from unittest.mock import MagicMock

import pytest
import requests
from streamlit.testing.v1 import AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "app.py")

DONE_RUN = {
    "id": "abc12345", "received_at": "2026-10-10T12:00:00+00:00", "status": "done",
    "payload": {"repo": "me/demo", "file_path": "etl/pipeline.py"},
    "error_log": "KeyError: 'total_price'", "analysis": "RCA",
    "original_code": 'df["revenue"] = df["total_price"] * df["quantity"]\n',
    "iteration": 2,
    "engineer": {"summary": "Use price.", "fixed_code": 'df["revenue"] = df["price"] * df["quantity"]\n',
                 "language": "Python", "valid": True, "error": None},
    "sast": {"passed": True, "escalate": False, "reasons": [],
             "layers": {"policy": "passed", "ast": "passed", "ruff": "passed", "bandit": "passed"}},
    "tests": {"passed": True, "skipped": False, "mode": "docker", "command": "python -m pytest -q",
              "reproduced": True, "before_output": "KeyError: 'total_price'", "output": "3 passed"},
    "attempts": [
        {"iteration": 1, "stage": "sast", "summary": "first", "diff": "-a\n+b\n",
         "feedback": "policy drop: DROP TABLE customers"},
        {"iteration": 2, "stage": "accepted", "summary": "Use price.", "diff": "-a\n+c\n", "feedback": None},
    ],
    "pr": {"pr_url": "https://github.com/me/demo/pull/1", "pr_number": 1, "branch": "autoheal/fix-x",
           "base_branch": "main", "reused": False, "draft": True, "labels": ["autoheal-bot"]},
    "issue": None,
    "error": None,
}


def render(monkeypatch, runs=None, error=None):
    def fake_get(url, timeout=None):
        if error:
            raise requests.ConnectionError(error)
        resp = MagicMock()
        resp.json.return_value = runs
        return resp
    monkeypatch.setattr(requests, "get", fake_get)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def code_blocks(at, language=None):
    return [c.value for c in at.code if language is None or c.language == language]


def test_backend_down(monkeypatch):
    at = render(monkeypatch, error="refused")
    assert any("Tidak bisa terhubung" in e.value for e in at.error)


def test_no_runs(monkeypatch):
    at = render(monkeypatch, runs=[])
    assert any("Belum ada run" in i.value for i in at.info)


def test_done_run_shows_diff_guardrails_tests_and_draft_pr(monkeypatch):
    at = render(monkeypatch, runs=[DONE_RUN])
    diff = code_blocks(at, "diff")[0]
    assert '-df["revenue"] = df["total_price"]' in diff and '+df["revenue"] = df["price"]' in diff
    successes = [s.value for s in at.success]
    assert any("Draft Pull Request #1" in s for s in successes)
    assert any("lolos semua lapisan" in s for s in successes)
    assert any("Test lolos setelah patch" in s for s in successes)
    assert any("| bandit | passed |" in m.value for m in at.markdown)
    assert any("DROP TABLE customers" in c for c in code_blocks(at))  # feedback of iteration 1
    assert [e.label for e in at.expander if "Iterasi" in e.label] == ["❌ Iterasi 1: SAST / policy",
                                                                      "✅ Iterasi 2: Lolos"]


def test_escalated_run_links_issue(monkeypatch):
    run = {**DONE_RUN, "status": "escalated", "pr": None, "error": "Escalated to a human: no valid fix",
           "issue": {"issue_url": "https://github.com/me/demo/issues/4", "issue_number": 4},
           "sast": {**DONE_RUN["sast"], "passed": False, "reasons": ["policy drop: x"]}}
    at = render(monkeypatch, runs=[run])
    assert any("mengeskalasi" in e.value for e in at.error)
    assert any("issues/4" in m.value for m in at.markdown)


def test_duplicate_run(monkeypatch):
    run = {**DONE_RUN, "status": "duplicate", "engineer": None, "sast": None, "tests": None, "attempts": [],
           "occurrences": 5, "pr": {"comment_url": "https://github.com/me/demo/pull/1#c"}}
    at = render(monkeypatch, runs=[run])
    assert any("x5" in i.value for i in at.info)


@pytest.mark.parametrize("run", [
    {**DONE_RUN, "status": "engineering", "engineer": None, "pr": None, "original_code": None, "sast": None,
     "tests": None, "attempts": []},
    {**DONE_RUN, "status": "testing", "pr": None},
    {**DONE_RUN, "status": "failed", "pr": None, "error": "Could not fetch"},
    {**DONE_RUN, "pr": {**DONE_RUN["pr"], "reused": True}},
    {**DONE_RUN, "engineer": {**DONE_RUN["engineer"], "valid": False, "error": "SyntaxError"}},
    {**DONE_RUN, "tests": {"passed": False, "skipped": True, "output": "Sandbox is off."}},
    {**DONE_RUN, "status": "done", "pr": {"error": "No PR opened: PR quota reached"}},
    # Older runs saved before V2 have none of the new fields
    {k: v for k, v in DONE_RUN.items() if k not in ("sast", "tests", "attempts", "iteration", "issue")},
])
def test_other_states_render(monkeypatch, run):
    render(monkeypatch, runs=[run])
