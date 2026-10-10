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
    "original_code": 'df["revenue"] = df["total_price"] * df["quantity"]\n',
    "engineer": {"summary": "Use price.", "fixed_code": 'df["revenue"] = df["price"] * df["quantity"]\n',
                 "language": "Python", "valid": True, "error": None},
    "pr": {"pr_url": "https://github.com/me/demo/pull/1", "pr_number": 1, "branch": "autoheal/fix-x",
           "base_branch": "main", "reused": False},
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


def test_backend_down(monkeypatch):
    at = render(monkeypatch, error="refused")
    assert any("Tidak bisa terhubung" in e.value for e in at.error)


def test_no_runs(monkeypatch):
    at = render(monkeypatch, runs=[])
    assert any("Belum ada run" in i.value for i in at.info)


def test_done_run_shows_diff_and_pr(monkeypatch):
    at = render(monkeypatch, runs=[DONE_RUN])
    diff = at.code[0].value
    assert '-df["revenue"] = df["total_price"]' in diff and '+df["revenue"] = df["price"]' in diff
    assert any("Pull Request #1" in s.value for s in at.success)


@pytest.mark.parametrize("run", [
    {**DONE_RUN, "status": "engineering", "engineer": None, "pr": None, "original_code": None},
    {**DONE_RUN, "status": "failed", "pr": None, "error": "Could not fetch"},
    {**DONE_RUN, "pr": {**DONE_RUN["pr"], "reused": True}},
    {**DONE_RUN, "engineer": {**DONE_RUN["engineer"], "valid": False, "error": "SyntaxError"}},
])
def test_other_states_render(monkeypatch, run):
    render(monkeypatch, runs=[run])
