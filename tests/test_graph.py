"""LangGraph pipeline (Sub-Task 11, 13, 15, 19) with the LLM, GitHub and the sandbox replaced by fakes."""
import pytest

from backend import graph, rate_limiter, run_store
from backend.contracts import RedactedLog, TestResult
from conftest import FIXED, ORIGINAL, PAYLOAD, engineer_result

DROP_PATCH = FIXED + '\n\ndef cleanup(conn):\n    conn.execute("DROP TABLE customers")\n'
FSTRING_PATCH = FIXED + '\n\ndef load(conn, table):\n    return conn.execute(f"SELECT * FROM {table}")\n'
FAILING_TESTS = TestResult(passed=False, output="KeyError: 'revenue'", mode="docker", reproduced=True,
                           before_output="KeyError: 'total_price'", command="python -m pytest -q")
PASSING_TESTS = TestResult(passed=True, output="3 passed", mode="docker", reproduced=True,
                           before_output="KeyError: 'total_price'", command="python -m pytest -q")


def run(payload=None, run_id=None):
    payload = payload or PAYLOAD
    run_id = run_id or run_store.create_run({k: v for k, v in payload.items() if k != "error_log"})["id"]
    final = graph.run_pipeline(run_id, payload)
    return final, run_store.get_run(run_id)


def test_happy_path_opens_draft_pr(fakes):
    fakes.test_results = [PASSING_TESTS]
    final, stored = run()

    assert final["outcome"] == "pr" and stored["status"] == "done"
    assert stored["pr"]["draft"] is True and stored["pr"]["labels"] == ["autoheal-bot"]
    pr = fakes.prs[0]
    assert pr["files"] == {"etl/pipeline.py": FIXED}
    report = pr["report"]
    assert report["iterations"] == 1
    assert report["sast"]["passed"] and report["sast"]["layers"]["bandit"] == "passed"
    assert report["tests"]["passed"] and report["tests"]["reproduced"]
    assert [a["stage"] for a in stored["attempts"]] == ["accepted"]


def test_sast_rejects_drop_table_then_engineer_fixes_it(fakes):
    """Plan V2 demo scenario 2: dangerous patch is rejected, the Engineer gets the reason and corrects it."""
    fakes.engineer_replies = [engineer_result(DROP_PATCH), engineer_result(FIXED)]
    final, stored = run()

    assert final["outcome"] == "pr" and final["iteration"] == 2
    assert fakes.engineer_calls[0]["feedback"] is None
    second = fakes.engineer_calls[1]
    assert "DROP TABLE customers" in second["feedback"] and second["previous_code"] == DROP_PATCH
    assert [a["stage"] for a in stored["attempts"]] == ["sast", "accepted"]
    assert fakes.prs[0]["files"]["etl/pipeline.py"] == FIXED


def test_sql_fstring_is_rejected(fakes):
    fakes.engineer_replies = [engineer_result(FSTRING_PATCH), engineer_result(FIXED)]
    run()
    assert "sql-fstring" in fakes.engineer_calls[1]["feedback"]


def test_sandbox_failure_feeds_back_then_passes(fakes):
    """Sub-Task 15 criterion: first patch fails the tests, the Engineer corrects itself, second patch passes."""
    fakes.test_results = [FAILING_TESTS, PASSING_TESTS]
    fakes.engineer_replies = [engineer_result(FIXED.replace("price", "unit_price")), engineer_result(FIXED)]
    final, stored = run()

    assert final["outcome"] == "pr" and final["iteration"] == 2
    assert "KeyError: 'revenue'" in fakes.engineer_calls[1]["feedback"]
    assert [a["stage"] for a in stored["attempts"]] == ["sandbox", "accepted"]
    assert fakes.prs[0]["report"]["iterations"] == 2


def test_escalates_after_three_failed_iterations(fakes):
    fakes.test_results = [FAILING_TESTS]
    final, stored = run()

    assert final["outcome"] == "escalated" and stored["status"] == "escalated"
    assert len(fakes.engineer_calls) == graph.MAX_ITERATIONS == 3
    assert fakes.prs == []
    repo, title, body = fakes.issues[0]
    assert repo == "me/demo" and "needs a human" in title
    assert "Attempt 3" in body and "RCA:" in body
    assert stored["issue"]["issue_number"] == 1


def test_invalid_engineer_output_counts_as_iteration(fakes):
    fakes.engineer_replies = [engineer_result(None, valid=False, error="no code block")]
    final, _ = run()
    assert final["outcome"] == "escalated" and len(fakes.engineer_calls) == 3


def test_hard_policy_violation_escalates_immediately(fakes, monkeypatch):
    monkeypatch.setattr(graph.guardrails, "MAX_CHANGED_LINES", 1)
    final, _ = run()
    assert final["outcome"] == "escalated" and len(fakes.engineer_calls) == 1
    assert "hard policy rule" in fakes.issues[0][1]


def test_twelve_webhooks_give_one_pr_and_eleven_comments(fakes):
    """Sub-Task 11 criterion (with the dedup stub): 12 identical failures within a minute."""
    outcomes = [run()[0]["outcome"] for _ in range(12)]

    assert outcomes.count("pr") == 1 and outcomes.count("duplicate") == 11
    assert len(fakes.prs) == 1 and len(fakes.comments) == 11
    assert len(fakes.researcher_inputs) == 1  # duplicates stop before any LLM call
    assert "Terjadi lagi (x12)" in fakes.comments[-1][2]


def test_pr_quota_per_repo(fakes, monkeypatch):
    monkeypatch.setattr(rate_limiter, "MAX_PRS_PER_HOUR", 2)
    outcomes = []
    for i in range(3):
        payload = {**PAYLOAD, "file_path": f"etl/job_{i}.py", "error_log": f"KeyError: 'col_{i}'"}
        outcomes.append(run(payload)[0]["outcome"])
    assert outcomes == ["pr", "pr", "rate_limited"]


def test_circuit_breaker_stops_agent_after_three_escalations(fakes):
    fakes.test_results = [FAILING_TESTS]
    for i in range(3):
        assert run({**PAYLOAD, "error_log": f"ValueError: bad row {chr(97 + i)}"})[0]["outcome"] == "escalated"
    assert rate_limiter.breaker_open("me/demo")

    calls_before = len(fakes.engineer_calls)
    final, stored = run({**PAYLOAD, "error_log": "TypeError: something new"})
    assert final["outcome"] == "failed" and "Circuit breaker" in stored["error"]
    assert len(fakes.engineer_calls) == calls_before

    rate_limiter.reset_breaker("me/demo")
    fakes.test_results = [PASSING_TESTS]
    assert run({**PAYLOAD, "error_log": "TypeError: something new"})[0]["outcome"] == "pr"


def test_slack_is_notified_on_escalation(fakes, monkeypatch):
    sent = []
    monkeypatch.setattr(graph.escalation, "notify_slack", lambda text: sent.append(text) or True)
    fakes.test_results = [FAILING_TESTS]
    _, stored = run()
    assert "gave up" in sent[0] and stored["issue"]["slack_notified"] is True


def test_only_redacted_log_reaches_llm_and_storage(fakes, monkeypatch):
    """Plan V2 demo scenario 1, with a fake redactor in place of Anggota A's module."""
    secret = "AKIAABCDEFGHIJKLMNOP"
    monkeypatch.setattr(graph, "redact", lambda raw: RedactedLog(raw.replace(secret, "<AWS_KEY_1>"), 1))
    payload = {**PAYLOAD, "error_log": f"botocore error key={secret}\nKeyError: 'total_price'"}
    _, stored = run(payload)

    assert secret not in fakes.researcher_inputs[0] and "<AWS_KEY_1>" in fakes.researcher_inputs[0]
    assert secret not in fakes.engineer_calls[0]["error"]
    assert secret not in str(stored) and stored["redaction_count"] == 1


def test_no_repo_means_no_pr(fakes):
    final, stored = run({"error_log": "KeyError: 'x'"})
    assert final["outcome"] == "no_pr" and "no 'repo'" in stored["pr"]["error"]


def test_fetch_failure_stops_before_engineer(fakes, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("404 Not Found")
    monkeypatch.setattr(graph, "fetch_file", boom)
    final, stored = run()
    assert final["outcome"] == "failed" and "Could not fetch" in stored["error"]
    assert fakes.engineer_calls == []


def test_engineer_gets_original_and_context(fakes, monkeypatch):
    from backend.contracts import ContextBundle
    monkeypatch.setattr(graph, "retrieve_context",
                        lambda path: ContextBundle(files={"main.py": "from etl.pipeline import transform\n"}))
    run()
    call = fakes.engineer_calls[0]
    assert call["original"] == ORIGINAL and call["context_files"] == {"main.py": "from etl.pipeline import transform\n"}


def test_checkpoints_and_resume(fakes, monkeypatch):
    """A crash in the middle leaves a checkpoint; resume continues from there without redoing research."""
    calls = {"n": 0}
    real = fakes._create_pr

    def crash_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("GitHub is down")
        return real(*args, **kwargs)
    monkeypatch.setattr(graph, "create_fix_pull_request", crash_once)
    run_id = run_store.create_run({})["id"]

    with pytest.raises(RuntimeError):
        graph.run_pipeline(run_id, PAYLOAD)
    history = graph.checkpoint_history(run_id)
    assert history[-1]["next"] == ["create_pr"]
    # The PR slot booked by the failed attempt was given back
    assert rate_limiter.status()["prs_last_hour"]["me/demo"] == 0

    final = graph.resume_pipeline(run_id)
    assert final["outcome"] == "pr" and len(fakes.researcher_inputs) == 1 and len(fakes.engineer_calls) == 1
