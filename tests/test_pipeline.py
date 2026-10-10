"""End-to-end flow through POST /webhook with the LLM and GitHub calls replaced by fakes."""
import pytest
from fastapi.testclient import TestClient

from backend import main

PAYLOAD = {"error_log": "KeyError: 'total_price'", "repo": "me/demo", "file_path": "etl/pipeline.py",
           "branch": "main", "run_url": "https://github.com/me/demo/actions/runs/1"}
ENGINEER_OK = {"summary": "Use price.", "fixed_code": "fixed\n", "language": "Python", "valid": True, "error": None}


@pytest.fixture
def calls(monkeypatch):
    calls = {}
    monkeypatch.setattr(main, "run_researcher", lambda err: "RCA text")
    monkeypatch.setattr(main, "fetch_file", lambda repo, path, ref=None: {"content": "original\n"})

    def fake_engineer(err, rca, path, original):
        calls["engineer"] = (err, rca, path, original)
        return ENGINEER_OK

    def fake_pr(repo, path, code, summary, **kwargs):
        calls["pr"] = (repo, path, code, summary, kwargs)
        return {"pr_url": "https://github.com/me/demo/pull/1", "pr_number": 1, "branch": "autoheal/fix-x",
                "base_branch": "main", "reused": False}

    monkeypatch.setattr(main, "run_engineer", fake_engineer)
    monkeypatch.setattr(main, "create_fix_pull_request", fake_pr)
    return calls


@pytest.fixture
def client():
    return TestClient(main.app)


def test_full_flow(client, calls):
    resp = client.post("/webhook", json=PAYLOAD)
    assert resp.status_code == 200
    run = client.get(f"/runs/{resp.json()['run_id']}").json()

    assert run["status"] == "done", run
    assert run["analysis"] == "RCA text"
    assert run["original_code"] == "original\n"
    assert run["pr"]["pr_number"] == 1
    assert calls["engineer"] == ("KeyError: 'total_price'", "RCA text", "etl/pipeline.py", "original\n")
    assert calls["pr"][4]["base_branch"] == "main" and calls["pr"][4]["run_url"] == PAYLOAD["run_url"]
    assert client.get("/runs").json()[0]["id"] == run["id"]


def test_fetch_failure_stops_before_engineer(client, calls, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("404 Not Found")
    monkeypatch.setattr(main, "fetch_file", boom)

    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed" and "Could not fetch" in run["error"]
    assert "engineer" not in calls and "pr" not in calls


def test_invalid_fix_does_not_open_pr(client, calls, monkeypatch):
    monkeypatch.setattr(main, "run_engineer", lambda *a: {**ENGINEER_OK, "valid": False, "error": "SyntaxError"})
    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "failed" and "SyntaxError" in run["error"]
    assert "pr" not in calls


def test_without_repo_no_pr(client, calls):
    run_id = client.post("/webhook", json={"error_log": "boom"}).json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "done" and "no 'repo'" in run["pr"]["error"]


def test_researcher_crash_marks_run_failed(client, calls, monkeypatch):
    def boom(err):
        raise RuntimeError("Nebius is down")
    monkeypatch.setattr(main, "run_researcher", boom)
    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["error"] == "Nebius is down"


@pytest.mark.parametrize("kwargs, status", [
    ({"content": "not json", "headers": {"Content-Type": "application/json"}}, 400),
    ({"json": ["a", "list"]}, 400),
    ({"json": {"repo": "me/demo"}}, 422),
])
def test_rejects_bad_payloads(client, calls, kwargs, status):
    assert client.post("/webhook", **kwargs).status_code == status


def test_webhook_secret(client, calls, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_WEBHOOK_SECRET", "s3cret")
    assert client.post("/webhook", json=PAYLOAD).status_code == 401
    assert client.post("/webhook", json=PAYLOAD, headers={"X-AutoHeal-Secret": "wrong"}).status_code == 401
    assert client.post("/webhook", json=PAYLOAD, headers={"X-AutoHeal-Secret": "s3cret"}).status_code == 200


def test_unknown_run_404(client):
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/pr-status").status_code == 404
