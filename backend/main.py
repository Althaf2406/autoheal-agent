import hmac
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from backend import run_store
from backend.agent_engineer import run_engineer
from backend.agent_researcher import run_researcher
from backend.github_integration import create_fix_pull_request, fetch_file, get_pull_request_status
from backend.redactor import redact

app = FastAPI(title="AutoHeal Webhook Backend")


def process_pipeline(run_id: str, payload: dict):
    """
    Full flow for one failed pipeline run:
    Agent Researcher (A) -> Agent Engineer (B) -> Auto-PR GitHub (B).
    Runs in the background so the webhook can answer GitHub Actions right away.
    """
    error_message = payload.get("error_log") or payload.get("error") or str(payload)
    repo = payload.get("repo")            # e.g. "username/autoheal-demo-etl"
    file_path = payload.get("file_path")  # e.g. "etl/pipeline.py"
    branch = payload.get("branch")        # branch where the failure happened
    run_url = payload.get("run_url")      # link to the failed GitHub Actions run
    run_store.update_run(run_id, error_log=error_message)

    try:
        # --- Agent Researcher (Anggota A) ---
        run_store.update_run(run_id, status="researching")
        analysis_result = run_researcher(error_message)
        run_store.update_run(run_id, analysis=analysis_result)

        # --- Agent Engineer (Anggota B) ---
        run_store.update_run(run_id, status="engineering")
        original_code = None
        if repo and file_path:
            try:
                original_code = fetch_file(repo, file_path, ref=branch)["content"]
            except Exception as e:
                # Without the real file the LLM would invent one, and the PR would overwrite the real code
                run_store.update_run(run_id, status="failed",
                                     error=f"Could not fetch {file_path} from {repo}: {e}")
                return
        engineer_result = run_engineer(error_message, analysis_result, file_path, original_code)
        run_store.update_run(run_id, engineer=engineer_result, original_code=original_code)

        if not engineer_result["valid"]:
            run_store.update_run(run_id, status="failed", error=f"Agent Engineer: {engineer_result['error']}")
            return

        # --- Auto-PR GitHub (Anggota B) ---
        if not (repo and file_path):
            run_store.update_run(run_id, status="done",
                                 pr={"error": "Payload has no 'repo' or 'file_path', so no PR was opened."})
            return
        run_store.update_run(run_id, status="creating_pr")
        pr_result = create_fix_pull_request(
            repo, file_path, engineer_result["fixed_code"], engineer_result["summary"],
            root_cause_analysis=analysis_result, base_branch=branch, run_url=run_url,
        )
        run_store.update_run(run_id, status="done", pr=pr_result)

    except Exception as e:
        print(f"❌ Pipeline failed for run {run_id}: {e}")
        run_store.update_run(run_id, status="failed", error=str(e))


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Endpoint untuk menerima payload JSON dari GitHub Actions.

    Expected fields: error_log, repo, file_path, branch, run_url (only error_log is required).
    """
    # Optional shared secret: if AUTOHEAL_WEBHOOK_SECRET is set, the caller must send the same value
    expected_secret = os.environ.get("AUTOHEAL_WEBHOOK_SECRET")
    if expected_secret:
        given_secret = request.headers.get("X-AutoHeal-Secret", "")
        if not hmac.compare_digest(given_secret, expected_secret):
            raise HTTPException(status_code=401, detail="Invalid or missing X-AutoHeal-Secret header")

    # Menerima raw JSON payload
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    if not (payload.get("error_log") or payload.get("error")):
        raise HTTPException(status_code=422, detail="Field 'error_log' is required")

    # AMANKAN DULU!
    error_key = "error_log" if payload.get("error_log") else "error"
    raw_error = payload[error_key]
    if raw_error:
        redact_res = redact(raw_error)
        payload[error_key] = redact_res.clean_text
        payload["redacted_entities"] = redact_res.entity_count

    print("=== Webhook Received ===")
    print(payload)
    print("========================")

    run = run_store.create_run(payload)
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
