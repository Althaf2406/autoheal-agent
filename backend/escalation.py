"""
Escalation to a human (Sub-Task 11 & 15, Anggota B): when the agent gives up after the maximum number
of iterations, or a patch breaks a hard rule, it opens a GitHub Issue with the RCA and every patch it tried,
and (optionally) posts to Slack. No PR is opened in that case.
"""
from __future__ import annotations

import os

import requests

MAX_DIFF_IN_ISSUE = 6000


def build_issue_title(file_path: str | None, reason: str) -> str:
    return f"AutoHeal needs a human: {file_path or 'pipeline'} ({reason})"


def build_issue_body(state: dict, reason: str) -> str:
    parts = [
        "## 🆘 AutoHeal could not fix this automatically",
        f"**Reason:** {reason}",
    ]
    if state.get("run_url"):
        parts.append(f"**Failed run:** {state['run_url']}")
    parts += ["### Error log (redacted)", f"```\n{(state.get('redacted_log') or '')[-3000:]}\n```"]
    if state.get("analysis"):
        parts += ["### Root Cause Analysis (Agent Researcher)", state["analysis"]]

    attempts = state.get("attempts") or []
    parts.append(f"### Patch attempts ({len(attempts)})")
    for attempt in attempts:
        diff = (attempt.get("diff") or "_no patch_")[:MAX_DIFF_IN_ISSUE]
        parts.append(
            f"<details><summary>Attempt {attempt.get('iteration')}: rejected by {attempt.get('stage')}</summary>\n\n"
            f"**Summary:** {attempt.get('summary') or '-'}\n\n"
            f"**Why it was rejected:**\n\n```\n{attempt.get('feedback') or '-'}\n```\n\n"
            f"```diff\n{diff}\n```\n</details>"
        )
    parts.append("_Opened by AutoHeal Data Agent. No Pull Request was created._")
    return "\n\n".join(parts)


def notify_slack(text: str) -> bool:
    """Post to SLACK_WEBHOOK_URL if it is set. Never raises; returns True when the message was sent."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        resp = requests.post(url, json={"text": text}, timeout=10)
        return resp.ok
    except requests.RequestException as e:
        print(f"⚠️ [Slack] Notification failed: {e}")
        return False
