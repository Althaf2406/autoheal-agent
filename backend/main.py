import hmac
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from backend import graph, rate_limiter, run_store
from backend.github_integration import get_pull_request_status
from backend.redactor import redact

app = FastAPI(title="AutoHeal Webhook Backend")


def process_pipeline(run_id: str, payload: dict):
    """
    Full flow for one failed pipeline run, as a LangGraph state machine (backend/graph.py):
    redact -> dedup -> research -> context -> engineer -> SAST -> sandbox -> Draft PR / escalation.
    Runs in the background so the webhook can answer GitHub Actions right away.
    """
    try:
        graph.run_pipeline(run_id, payload)
    except Exception as e:
        print(f"❌ Pipeline failed for run {run_id}: {e}")
        run_store.update_run(run_id, status="failed", error=str(e))


def _check_secret(request: Request) -> None:
    # Optional shared secret: if AUTOHEAL_WEBHOOK_SECRET is set, the caller must send the same value
    expected_secret = os.environ.get("AUTOHEAL_WEBHOOK_SECRET")
    if expected_secret:
        given_secret = request.headers.get("X-AutoHeal-Secret", "")
        if not hmac.compare_digest(given_secret, expected_secret):
            raise HTTPException(status_code=401, detail="Invalid or missing X-AutoHeal-Secret header")


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint untuk menerima payload JSON dari GitHub Actions.

    Expected fields: error_log, repo, file_path, branch, commit, run_url (only error_log is required).
    """
    _check_secret(request)

    # Menerima raw JSON payload
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    if not (payload.get("error_log") or payload.get("error")):
        raise HTTPException(status_code=422, detail="Field 'error_log' is required")

    # The raw log may contain secrets or PII: it is not printed or stored, only the redacted text is (graph.node_redact)
    metadata = {k: v for k, v in payload.items() if k not in ("error_log", "error")}
    print(f"=== Webhook Received === {metadata}")

    run = run_store.create_run(metadata)
    background_tasks.add_task(process_pipeline, run["id"], payload)

    return {
        "status": "Success",
        "message": "Webhook received successfully. Agents are working in the background.",
        "run_id": run["id"],
    }


@app.get("/runs")
def list_runs():
    """All runs, newest first. Used by the Streamlit dashboard."""
    return run_store.list_runs()


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/runs/{run_id}/pr-status")
def pr_status(run_id: str):
    """Live PR state from GitHub (open / closed / merged)."""
    run = run_store.get_run(run_id)
    if not run or not run.get("pr") or not run["pr"].get("pr_number"):
        raise HTTPException(status_code=404, detail="No Pull Request for this run")
    return get_pull_request_status(run["payload"]["repo"], run["pr"]["pr_number"])


@app.get("/runs/{run_id}/history")
def run_history(run_id: str):
    """Checkpointed LangGraph steps of a run (audit trail)."""
    if not run_store.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return graph.checkpoint_history(run_id)


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str, request: Request, background_tasks: BackgroundTasks):
    """Continue a run that stopped mid-way (e.g. backend restart) from its last checkpoint."""
    _check_secret(request)
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not graph.checkpoint_history(run_id):
        raise HTTPException(status_code=409, detail="This run has no checkpoint to resume from")
    run_store.update_run(run_id, status="resuming", error=None)

    def resume():
        try:
            graph.resume_pipeline(run_id)
        except Exception as e:
            run_store.update_run(run_id, status="failed", error=str(e))
    background_tasks.add_task(resume)
    return {"status": "resuming", "run_id": run_id}


@app.get("/rate-limits")
def rate_limits():
    """PR quota, consecutive failures and circuit breaker state per repo."""
    return rate_limiter.status()


@app.post("/circuit-breaker/{owner}/{name}/reset")
def reset_circuit_breaker(owner: str, name: str, request: Request):
    """A human looked at the escalation issue and lets the agent work on this repo again."""
    _check_secret(request)
    rate_limiter.reset_breaker(f"{owner}/{name}")
    return rate_limiter.status()
