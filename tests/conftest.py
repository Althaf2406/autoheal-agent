import os
import sys

import pytest

# Some modules read the key at import time in older versions; a dummy keeps tests offline-safe
os.environ.setdefault("NEBIUS_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from backend import dedup, graph, rate_limiter, run_store  # noqa: E402

ORIGINAL = ('import pandas as pd\n\n\ndef transform(df):\n'
            '    df["revenue"] = df["total_price"] * df["quantity"]\n    return df\n')
FIXED = ('import pandas as pd\n\n\ndef transform(df):\n'
         '    df["revenue"] = df["price"] * df["quantity"]\n    return df\n')
PAYLOAD = {"error_log": "Traceback (most recent call last):\nKeyError: 'total_price'", "repo": "me/demo",
           "file_path": "etl/pipeline.py", "branch": "main", "commit": "abc123",
           "run_url": "https://github.com/me/demo/actions/runs/1"}


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep tests away from the real data/ files, from Docker and from secrets in a local .env."""
    monkeypatch.setattr(run_store, "STORE_PATH", str(tmp_path / "runs.json"))
    monkeypatch.setattr(run_store, "_runs", {})
    monkeypatch.setattr(rate_limiter, "STORE_PATH", str(tmp_path / "rate_limiter.json"))
    rate_limiter.reset()
    dedup.reset()
    monkeypatch.setattr(rate_limiter, "_sleep", lambda seconds: None)
    monkeypatch.setattr(graph, "_graph", graph.build_graph(InMemorySaver()))
    monkeypatch.setenv("AUTOHEAL_SANDBOX", "off")
    for name in ("AUTOHEAL_WEBHOOK_SECRET", "SLACK_WEBHOOK_URL"):
        monkeypatch.delenv(name, raising=False)


def engineer_result(code=FIXED, summary="Use price instead of total_price.", valid=True, error=None):
    return {"summary": summary, "fixed_code": code, "language": "Python", "valid": valid, "error": error}


class Fakes:
    """Replaces every outside call of the pipeline (LLM, GitHub, sandbox) and records what was sent."""

    def __init__(self, monkeypatch):
        self.engineer_replies = []   # queued engineer results; the last one repeats
        self.test_results = []       # queued sandbox results; the last one repeats (None = sandbox off)
        self.researcher_inputs, self.engineer_calls, self.prs, self.comments, self.issues = [], [], [], [], []
        self.next_pr_number = 1
        self.open_pr = None
        monkeypatch.setattr(graph, "run_researcher", self._research)
        monkeypatch.setattr(graph, "fetch_file", lambda repo, path, ref=None: {"content": ORIGINAL})
        monkeypatch.setattr(graph, "run_engineer", self._engineer)
        monkeypatch.setattr(graph, "create_fix_pull_request", self._create_pr)
        monkeypatch.setattr(graph, "comment_on_pull_request", self._comment)
        monkeypatch.setattr(graph, "create_escalation_issue", self._issue)
        monkeypatch.setattr(graph, "get_clone_url", lambda repo: f"https://x-access-token:SECRET@github.com/{repo}.git")
        monkeypatch.setattr(graph.sandbox, "run_tests", self._run_tests)

    def _research(self, log):
        self.researcher_inputs.append(log)
        return "RCA: the CSV has price, not total_price."

    def _engineer(self, error, rca, path, original, **kwargs):
        self.engineer_calls.append({"error": error, "rca": rca, "path": path, "original": original, **kwargs})
        reply = self.engineer_replies.pop(0) if len(self.engineer_replies) > 1 else (
            self.engineer_replies[0] if self.engineer_replies else engineer_result())
        return reply

    def _run_tests(self, url, sha, patch, file_path=None):
        from backend.contracts import TestResult
        if not self.test_results:
            return TestResult(passed=False, skipped=True, mode="off", output="Sandbox is off.")
        result = self.test_results.pop(0) if len(self.test_results) > 1 else self.test_results[0]
        return result

    def _create_pr(self, repo, files, summary, **kwargs):
        if self.open_pr:
            return {**self.open_pr, "reused": True}
        number = self.next_pr_number
        self.next_pr_number += 1
        pr = {"pr_url": f"https://github.com/{repo}/pull/{number}", "pr_number": number,
              "branch": f"autoheal/fix-{number}", "base_branch": kwargs.get("base_branch") or "main",
              "reused": False, "draft": True, "labels": ["autoheal-bot"]}
        self.prs.append({"repo": repo, "files": files, "summary": summary, **kwargs})
        return pr

    def _comment(self, repo, number, body):
        self.comments.append((repo, number, body))
        return f"https://github.com/{repo}/pull/{number}#comment-{len(self.comments)}"

    def _issue(self, repo, title, body):
        self.issues.append((repo, title, body))
        return {"issue_url": f"https://github.com/{repo}/issues/{len(self.issues)}", "issue_number": len(self.issues)}


@pytest.fixture
def fakes(monkeypatch):
    return Fakes(monkeypatch)
