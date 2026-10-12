"""End-to-end flow through the FastAPI endpoints, with the LLM, GitHub and the sandbox replaced by fakes."""
import pytest
from fastapi.testclient import TestClient

from backend import graph, main, rate_limiter
from conftest import PAYLOAD


@pytest.fixture
def client():
    return TestClient(main.app)


def test_full_flow(client, fakes):
    resp = client.post("/webhook", json=PAYLOAD)
    assert resp.status_code == 200
    run = client.get(f"/runs/{resp.json()['run_id']}").json()

    assert run["status"] == "done", run
    assert run["analysis"].startswith("RCA")
    assert run["pr"]["pr_number"] == 1 and run["pr"]["draft"] is True
    assert run["sast"]["passed"] and run["tests"]["skipped"]
    assert run["iteration"] == 1 and run["attempts"][0]["stage"] == "accepted"
    assert fakes.prs[0]["base_branch"] == "main" and fakes.prs[0]["run_url"] == PAYLOAD["run_url"]
    assert client.get("/runs").json()[0]["id"] == run["id"]


def test_raw_log_is_not_stored_in_payload(client, fakes):
    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert "error_log" not in run["payload"] and run["payload"]["repo"] == "me/demo"


def test_history_endpoint(client, fakes):
    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    steps = client.get(f"/runs/{run_id}/history").json()
    assert len(steps) > 5 and steps[-1]["outcome"] == "pr"


def test_resume_endpoint(client, fakes, monkeypatch):
    real = fakes._create_pr
    state = {"crash": True}

    def flaky(*args, **kwargs):
        if state.pop("crash", False):
            raise RuntimeError("GitHub is down")
        return real(*args, **kwargs)
    monkeypatch.setattr(graph, "create_fix_pull_request", flaky)

    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "failed"
    assert client.post(f"/runs/{run_id}/resume").status_code == 200
    run = client.get(f"/runs/{run_id}").json()
    assert run["status"] == "done" and run["pr"]["pr_number"] == 1


def test_resume_without_checkpoint(client):
    run_id = main.run_store.create_run({})["id"]
    assert client.post(f"/runs/{run_id}/resume").status_code == 409


def test_crash_marks_run_failed(client, fakes, monkeypatch):
    def boom(err):
        raise RuntimeError("Nebius is down")
    monkeypatch.setattr(graph, "run_researcher", boom)
    run_id = client.post("/webhook", json=PAYLOAD).json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["error"] == "Nebius is down"


def test_rate_limits_and_breaker_reset(client):
    for _ in range(3):
        rate_limiter.record_fix_result("me/demo", False)
    assert client.get("/rate-limits").json()["breaker_open"] == ["me/demo"]
    assert client.post("/circuit-breaker/me/demo/reset").json()["breaker_open"] == []


@pytest.mark.parametrize("kwargs, status", [
    ({"content": "not json", "headers": {"Content-Type": "application/json"}}, 400),
    ({"json": ["a", "list"]}, 400),
    ({"json": {"repo": "me/demo"}}, 422),
])
def test_rejects_bad_payloads(client, fakes, kwargs, status):
    assert client.post("/webhook", **kwargs).status_code == status


def test_webhook_secret(client, fakes, monkeypatch):
    monkeypatch.setenv("AUTOHEAL_WEBHOOK_SECRET", "s3cret")
    assert client.post("/webhook", json=PAYLOAD).status_code == 401
    assert client.post("/webhook", json=PAYLOAD, headers={"X-AutoHeal-Secret": "wrong"}).status_code == 401
    assert client.post("/webhook", json=PAYLOAD, headers={"X-AutoHeal-Secret": "s3cret"}).status_code == 200
    # Admin endpoints are protected by the same secret
    assert client.post("/circuit-breaker/me/demo/reset").status_code == 401


def test_unknown_run_404(client):
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/pr-status").status_code == 404
    assert client.get("/runs/nope/history").status_code == 404
    assert client.post("/runs/nope/resume").status_code == 404


def test_metrics_endpoint(client, fakes):
    resp = client.post("/webhook", json=PAYLOAD)
    assert resp.status_code == 200

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    data = metrics_resp.json()
    assert data["total_runs"] >= 1
    assert data["done_runs"] >= 1
    assert "success_rate_pct" in data
    assert "avg_iterations" in data
    assert "sast_rejections" in data

