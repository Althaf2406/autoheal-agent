r"""
LangGraph orchestration (Sub-Task 15, shared).

    redact -> dedup -> research -> retrieve_context -> engineer -> sast -> sandbox_test -> create_pr
                 \                                       ^          |          |
                  -> comment_duplicate                   |__________|__________|  (max 3 iterations)
                                                                    \__________\__ -> escalate (Issue)

Anggota A: redact, dedup, research, retrieve_context.  Anggota B: engineer, sast, sandbox_test,
create_pr, escalate. Anggota A's functions come from their modules when those exist (backend/redactor.py,
backend/dedup.py, backend/context_retriever.py), otherwise from the stubs in backend/contracts.py.

Every step is checkpointed (SQLite in data/checkpoints.sqlite, thread id = run id) so a run can be
inspected or resumed, and mirrored into run_store for the dashboard.
"""
from __future__ import annotations

import importlib
import operator
import os
import sqlite3
from dataclasses import asdict
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend import contracts, escalation, guardrails, rate_limiter, run_store, sandbox
from backend.agent_engineer import run_engineer
from backend.agent_researcher import run_researcher
from backend.github_integration import (comment_on_pull_request, create_escalation_issue, create_fix_pull_request,
                                        fetch_file, get_clone_url)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKPOINT_PATH = os.path.join(PROJECT_ROOT, "data", "checkpoints.sqlite")
MAX_ITERATIONS = int(os.environ.get("AUTOHEAL_MAX_ITERATIONS", "3"))


def _from_anggota_a(module: str, name: str, fallback):
    try:
        return getattr(importlib.import_module(f"backend.{module}"), name)
    except (ImportError, AttributeError):
        return fallback


redact = _from_anggota_a("redactor", "redact", contracts.redact_stub)
acquire = _from_anggota_a("dedup", "acquire", contracts.acquire_stub)
fingerprint = _from_anggota_a("dedup", "fingerprint", contracts.fingerprint_stub)
retrieve_context = _from_anggota_a("context_retriever", "retrieve_context", contracts.retrieve_context_stub)
check_impact = _from_anggota_a("context_retriever", "check_impact", contracts.check_impact_stub)


class PipelineState(TypedDict, total=False):
    run_id: str
    repo: str | None
    file_path: str | None
    branch: str | None
    commit: str | None
    run_url: str | None
    error_log: str
    redacted_log: str
    redaction_count: int
    fingerprint: str
    occurrences: int
    analysis: str
    original_code: str | None
    context_files: dict[str, str]
    iteration: int
    engineer: dict
    new_files: dict[str, str]
    diff: str
    sast: dict
    tests: dict
    feedback: str | None
    escalate_reason: str | None
    attempts: Annotated[list[dict], operator.add]
    pr: dict
    issue: dict
    outcome: str  # pr | no_pr | duplicate | escalated | rate_limited | failed
    error: str | None


def _update(state: PipelineState, **fields) -> None:
    run_store.update_run(state["run_id"], **fields)


# ---------------------------------------------------------------------------
# Anggota A nodes
# ---------------------------------------------------------------------------

def node_redact(state: PipelineState) -> dict:
    _update(state, status="redacting")
    result = redact(state["error_log"])
    # Only the redacted text is stored or sent anywhere from here on
    _update(state, error_log=result.text, redaction_count=result.entity_count)
    return {"redacted_log": result.text, "redaction_count": result.entity_count}


def node_dedup(state: PipelineState) -> dict:
    fp = fingerprint(state.get("repo") or "", state.get("file_path") or "", state["redacted_log"])
    occurrences = rate_limiter.record_occurrence(fp)
    _update(state, fingerprint=fp, occurrences=occurrences)
    if state.get("repo") and rate_limiter.breaker_open(state["repo"]):
        error = f"Circuit breaker is open for {state['repo']}: too many failed fixes in a row. Waiting for a human."
        _update(state, status="failed", error=error)
        return {"fingerprint": fp, "occurrences": occurrences, "outcome": "failed", "error": error}
    duplicate = rate_limiter.in_cooldown(fp) or not acquire(fp)
    return {"fingerprint": fp, "occurrences": occurrences, "outcome": "duplicate" if duplicate else ""}


def node_research(state: PipelineState) -> dict:
    _update(state, status="researching")
    analysis = run_researcher(state["redacted_log"])
    _update(state, analysis=analysis)
    return {"analysis": analysis}


def node_retrieve_context(state: PipelineState) -> dict:
    original_code = None
    if state.get("repo") and state.get("file_path"):
        try:
            original_code = fetch_file(state["repo"], state["file_path"], ref=state.get("branch"))["content"]
        except Exception as e:
            # Without the real file the LLM would invent one, and the PR would overwrite the real code
            error = f"Could not fetch {state['file_path']} from {state['repo']}: {e}"
            _update(state, status="failed", error=error)
            return {"outcome": "failed", "error": error}
    bundle = retrieve_context(state["file_path"]) if state.get("file_path") else contracts.ContextBundle()
    _update(state, original_code=original_code)
    return {"original_code": original_code, "context_files": dict(bundle.files), "iteration": 0}


# ---------------------------------------------------------------------------
# Anggota B nodes
# ---------------------------------------------------------------------------

def node_engineer(state: PipelineState) -> dict:
    iteration = state.get("iteration", 0) + 1
    _update(state, status="engineering", iteration=iteration)
    previous = state.get("new_files", {}).get(state.get("file_path") or "") if state.get("feedback") else None
    result = run_engineer(state["redacted_log"], state.get("analysis", ""), state.get("file_path"),
                          state.get("original_code"), feedback=state.get("feedback"), previous_code=previous,
                          context_files=state.get("context_files"))
    _update(state, engineer=result)
    update: dict[str, Any] = {"iteration": iteration, "engineer": result}
    if not result["valid"]:
        feedback = f"Agent Engineer could not produce a valid file: {result['error']}"
        update.update(feedback=feedback, attempts=_attempt(state, iteration, "engineer", result, "", feedback))
        return update
    path = state.get("file_path") or "fix.py"
    new_files = {path: result["fixed_code"]}
    update.update(new_files=new_files, diff=guardrails.make_diff({path: state.get("original_code")}, new_files))
    return update


def node_sast(state: PipelineState) -> dict:
    _update(state, status="validating")
    path = next(iter(state["new_files"]))
    result = guardrails.validate_patch(state["diff"], state["new_files"], {path: state.get("original_code")},
                                       check_impact=check_impact)
    sast = asdict(result)
    _update(state, sast=sast)
    if result.passed:
        return {"sast": sast, "feedback": None}
    feedback = guardrails.format_feedback(result)
    return {"sast": sast, "feedback": feedback,
            "escalate_reason": "patch broke a hard policy rule" if result.escalate else None,
            "attempts": _attempt(state, state["iteration"], "sast", state["engineer"], state["diff"], feedback)}


def node_sandbox_test(state: PipelineState) -> dict:
    _update(state, status="testing")
    if state.get("repo"):
        try:
            url = get_clone_url(state["repo"])
            result = sandbox.run_tests(url, state.get("commit") or state.get("branch") or "HEAD", state["diff"],
                                       file_path=state.get("file_path"))
        except Exception as e:
            result = contracts.TestResult(passed=False, skipped=True, output=f"Sandbox could not start: {e}")
    else:
        result = contracts.TestResult(passed=False, skipped=True, output="No repo in the payload.")
    tests = asdict(result)
    _update(state, tests=tests)
    if result.passed or result.skipped:
        return {"tests": tests, "feedback": None,
                "attempts": _attempt(state, state["iteration"], "accepted", state["engineer"], state["diff"], None)}
    feedback = sandbox.format_feedback(result)
    return {"tests": tests, "feedback": feedback,
            "attempts": _attempt(state, state["iteration"], "sandbox", state["engineer"], state["diff"], feedback)}


def node_create_pr(state: PipelineState) -> dict:
    repo, fp = state.get("repo"), state["fingerprint"]
    if not (repo and state.get("file_path")):
        pr = {"error": "Payload has no 'repo' or 'file_path', so no PR was opened."}
        _update(state, status="done", pr=pr)
        return {"pr": pr, "outcome": "no_pr"}

    _update(state, status="creating_pr")
    allowed, reason = rate_limiter.check_pr_allowed(repo, fp)
    if not allowed:
        pr = {"error": f"No PR opened: {reason}."}
        _comment_existing_pr(state)
        _update(state, status="done", pr=pr)
        return {"pr": pr, "outcome": "rate_limited"}

    report = {"iterations": state["iteration"], "sast": state.get("sast"), "tests": state.get("tests")}
    try:
        pr = create_fix_pull_request(repo, state["new_files"], state["engineer"]["summary"],
                                     root_cause_analysis=state.get("analysis", ""),
                                     base_branch=state.get("branch"), run_url=state.get("run_url"), report=report)
    except Exception:
        rate_limiter.release_pr_slot(repo, fp)
        raise
    rate_limiter.remember_pr(fp, pr["pr_number"])
    rate_limiter.record_fix_result(repo, success=True)
    if pr.get("reused"):
        pr["comment_url"] = _comment_existing_pr(state, pr["pr_number"])
    _update(state, status="done", pr=pr)
    return {"pr": pr, "outcome": "pr"}


def node_escalate(state: PipelineState) -> dict:
    reason = state.get("escalate_reason") or f"no valid fix after {state.get('iteration', 0)} iterations"
    _update(state, status="escalating")
    breaker_opened = rate_limiter.record_fix_result(state["repo"], success=False) if state.get("repo") else False
    issue = {}
    if state.get("repo"):
        try:
            issue = create_escalation_issue(state["repo"], escalation.build_issue_title(state.get("file_path"), reason),
                                            escalation.build_issue_body(state, reason))
        except Exception as e:
            issue = {"error": f"Could not open the escalation issue: {e}"}
    text = f"🆘 AutoHeal gave up on {state.get('repo') or '-'} / {state.get('file_path') or '-'}: {reason}."
    if issue.get("issue_url"):
        text += f" Issue: {issue['issue_url']}"
    if breaker_opened:
        text += " The circuit breaker is now OPEN for this repo; the agent will stop until it is reset."
    issue["slack_notified"] = escalation.notify_slack(text)
    _update(state, status="escalated", issue=issue, error=f"Escalated to a human: {reason}")
    return {"issue": issue, "outcome": "escalated"}


def node_comment_duplicate(state: PipelineState) -> dict:
    comment_url = _comment_existing_pr(state)
    _update(state, status="duplicate", pr={"comment_url": comment_url} if comment_url else None,
            error=None)
    return {"outcome": "duplicate"}


# ---------------------------------------------------------------------------
# Helpers & routing
# ---------------------------------------------------------------------------

def _attempt(state: PipelineState, iteration: int, stage: str, engineer: dict, diff: str,
             feedback: str | None) -> list[dict]:
    """One entry for the audit trail (dashboard, escalation issue). Returned as a list for the state reducer."""
    attempt = {"iteration": iteration, "stage": stage, "summary": (engineer or {}).get("summary"),
               "diff": diff, "feedback": feedback}
    _update(state, attempts=list(state.get("attempts") or []) + [attempt])
    return [attempt]


def _comment_existing_pr(state: PipelineState, pr_number: int | None = None) -> str | None:
    """'Terjadi lagi (xN)' on the PR that already handles this error, if there is one."""
    repo = state.get("repo")
    pr_number = pr_number or rate_limiter.pr_for_fingerprint(state.get("fingerprint", ""))
    if not (repo and pr_number):
        return None
    body = (f"🔁 Terjadi lagi (x{state.get('occurrences', 1)}). The same error was reported again"
            + (f" in {state['run_url']}" if state.get("run_url") else "") + ". No new PR was opened.")
    try:
        return comment_on_pull_request(repo, pr_number, body)
    except Exception as e:
        print(f"⚠️ [GitHub] Could not comment on PR #{pr_number}: {e}")
        return None


def route_after_dedup(state: PipelineState) -> str:
    if state.get("outcome") == "failed":
        return END
    return "comment_duplicate" if state.get("outcome") == "duplicate" else "research"


def route_after_context(state: PipelineState) -> str:
    return END if state.get("outcome") == "failed" else "engineer"


def _retry_or_escalate(state: PipelineState) -> str:
    if state.get("escalate_reason"):
        return "escalate"
    return "engineer" if state.get("iteration", 0) < MAX_ITERATIONS else "escalate"


def route_after_engineer(state: PipelineState) -> str:
    return "sast" if state["engineer"]["valid"] else _retry_or_escalate(state)


def route_after_sast(state: PipelineState) -> str:
    return "sandbox_test" if state["sast"]["passed"] else _retry_or_escalate(state)


def route_after_sandbox(state: PipelineState) -> str:
    tests = state["tests"]
    return "create_pr" if tests["passed"] or tests["skipped"] else _retry_or_escalate(state)


def build_graph(checkpointer=None):
    g = StateGraph(PipelineState)
    for name, fn in (("redact", node_redact), ("dedup", node_dedup), ("research", node_research),
                     ("retrieve_context", node_retrieve_context), ("engineer", node_engineer),
                     ("sast", node_sast), ("sandbox_test", node_sandbox_test), ("create_pr", node_create_pr),
                     ("escalate", node_escalate), ("comment_duplicate", node_comment_duplicate)):
        g.add_node(name, fn)
    g.add_edge(START, "redact")
    g.add_edge("redact", "dedup")
    g.add_conditional_edges("dedup", route_after_dedup, ["research", "comment_duplicate", END])
    g.add_edge("research", "retrieve_context")
    g.add_conditional_edges("retrieve_context", route_after_context, ["engineer", END])
    g.add_conditional_edges("engineer", route_after_engineer, ["sast", "engineer", "escalate"])
    g.add_conditional_edges("sast", route_after_sast, ["sandbox_test", "engineer", "escalate"])
    g.add_conditional_edges("sandbox_test", route_after_sandbox, ["create_pr", "engineer", "escalate"])
    for name in ("create_pr", "escalate", "comment_duplicate"):
        g.add_edge(name, END)
    return g.compile(checkpointer=checkpointer)


_graph = None


def get_graph():
    """Compiled graph with a SQLite checkpointer (created on first use)."""
    global _graph
    if _graph is None:
        from langgraph.checkpoint.sqlite import SqliteSaver
        os.makedirs(os.path.dirname(CHECKPOINT_PATH), exist_ok=True)
        conn = sqlite3.connect(CHECKPOINT_PATH, check_same_thread=False)
        _graph = build_graph(SqliteSaver(conn))
    return _graph


def initial_state(run_id: str, payload: dict) -> PipelineState:
    return {
        "run_id": run_id,
        "repo": payload.get("repo"),
        "file_path": payload.get("file_path"),
        "branch": payload.get("branch"),
        "commit": payload.get("commit"),
        "run_url": payload.get("run_url"),
        "error_log": payload.get("error_log") or payload.get("error") or "",
        "attempts": [],
    }


def run_pipeline(run_id: str, payload: dict) -> PipelineState:
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}
    return get_graph().invoke(initial_state(run_id, payload), config)


def resume_pipeline(run_id: str) -> PipelineState:
    """Continue a run from its last checkpoint (e.g. after the backend crashed mid-run)."""
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}
    return get_graph().invoke(None, config)


def checkpoint_history(run_id: str) -> list[dict]:
    """Audit trail: which node ran, in order, with the iteration at that point."""
    config = {"configurable": {"thread_id": run_id}}
    history = []
    for snapshot in get_graph().get_state_history(config):
        history.append({"step": snapshot.metadata.get("step"), "next": list(snapshot.next),
                        "iteration": snapshot.values.get("iteration"), "outcome": snapshot.values.get("outcome"),
                        "created_at": snapshot.created_at})
    return list(reversed(history))
