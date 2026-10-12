"""
GitHub integration for Agent Engineer (Sub-Task 2, 5 & 19).

PR governance (V2): every PR is a Draft with the label "autoheal-bot", must be reviewed by a human
(CODEOWNERS in the target repo) and is never merged by the bot. The PR body carries the RCA, the SAST
result, pytest before/after the patch, the changed files and the number of iterations.

Authentication, in order of preference:
  1. GitHub App: GITHUB_APP_ID + private key (GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY).
     The installation token for the target repo is requested automatically.
  2. Personal Access Token: GITHUB_TOKEN (handy for testing before the App is set up).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from github import Auth, Github, GithubException, GithubIntegration, InputGitTreeElement

from backend import rate_limiter

load_dotenv(override=True)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCH_PREFIX = "autoheal/fix-"
PR_TITLE_PREFIX = "AutoHeal: fix failing pipeline in "
BOT_LABEL = "autoheal-bot"
ESCALATION_LABEL = "needs-human"


class GitHubConfigError(Exception):
    """Raised when no usable GitHub credentials are configured."""


def _load_private_key() -> str | None:
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if key_path and not os.path.isabs(key_path):
        # Relative paths in .env are relative to the project root, not the current directory
        key_path = os.path.join(PROJECT_ROOT, key_path)
    if key_path and os.path.exists(key_path):
        with open(key_path, "r", encoding="utf-8") as f:
            return f.read()
    key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if key:
        # Allow the key to be stored on one line in .env with literal "\n"
        return key.replace("\\n", "\n")
    return None


def auth_mode() -> str:
    """Return "app", "token" or "none" depending on which credentials are set."""
    if os.environ.get("GITHUB_APP_ID") and _load_private_key():
        return "app"
    if os.environ.get("GITHUB_TOKEN"):
        return "token"
    return "none"


def _app_installation(repo_full_name: str):
    app_id = os.environ["GITHUB_APP_ID"].strip()
    if not app_id.isdigit():
        raise GitHubConfigError(f"GITHUB_APP_ID must be a number, got {app_id!r}.")
    app_auth = Auth.AppAuth(int(app_id), _load_private_key())
    integration = GithubIntegration(auth=app_auth)
    owner, name = repo_full_name.split("/", 1)
    try:
        return integration, integration.get_repo_installation(owner, name)
    except GithubException as e:
        raise GitHubConfigError(
            f"The GitHub App is not installed on {repo_full_name}. Install it from the App settings page."
        ) from e


def get_github_client(repo_full_name: str) -> Github:
    """Return an authenticated PyGithub client that can write to repo_full_name ("owner/name")."""
    mode = auth_mode()
    if mode == "app":
        return _app_installation(repo_full_name)[1].get_github_for_installation()
    if mode == "token":
        return Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
    raise GitHubConfigError(
        "No GitHub credentials found. Set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH, or GITHUB_TOKEN, in .env."
    )


def get_clone_url(repo_full_name: str) -> str:
    """HTTPS clone URL with a token, for the sandbox. Never log it (sandbox._scrub hides it)."""
    mode = auth_mode()
    if mode == "app":
        integration, installation = _app_installation(repo_full_name)
        token = integration.get_access_token(installation.id).token
    elif mode == "token":
        token = os.environ["GITHUB_TOKEN"]
    else:
        raise GitHubConfigError("No GitHub credentials found, the sandbox cannot clone the repo.")
    return f"https://x-access-token:{token}@github.com/{repo_full_name}.git"


def _repo(repo_full_name: str):
    gh = get_github_client(repo_full_name)
    rate_limiter.throttle_github(gh)
    return gh.get_repo(repo_full_name)


def fetch_file(repo_full_name: str, file_path: str, ref: str | None = None) -> dict:
    """Read a file from the repo. Returns {"content", "sha", "ref"}."""
    repo = _repo(repo_full_name)
    ref = ref or repo.default_branch
    contents = repo.get_contents(file_path, ref=ref)
    return {"content": contents.decoded_content.decode("utf-8"), "sha": contents.sha, "ref": ref}


def find_open_fix_pull_request(repo, file_path: str, base_branch: str):
    """Return an AutoHeal PR that is still open for this file and base branch, or None."""
    for pr in repo.get_pulls(state="open", base=base_branch):
        if pr.head.ref.startswith(BRANCH_PREFIX) and pr.title == f"{PR_TITLE_PREFIX}{file_path}":
            return pr
    return None


def create_fix_pull_request(repo_full_name: str, files: dict[str, str], summary: str,
                            root_cause_analysis: str = "", base_branch: str | None = None,
                            run_url: str | None = None, report: dict | None = None) -> dict:
    """
    Commit the fixed files (one commit, any number of files) on a new branch and open a Draft PR
    labelled autoheal-bot. If an AutoHeal PR for the same main file is still open, that PR is returned
    instead of opening a duplicate.

    `files` maps path -> new content; the first path names the PR.
    `report` holds the verification details for the PR body (see build_pr_body).

    Returns {"pr_url", "pr_number", "branch", "base_branch", "reused", "draft", "labels"}.
    """
    repo = _repo(repo_full_name)
    base_branch = base_branch or repo.default_branch
    main_file = next(iter(files))

    # 0. Every failed run on main triggers the agent; don't stack PRs while one is waiting for review
    open_pr = find_open_fix_pull_request(repo, main_file, base_branch)
    if open_pr:
        print(f"♻️ [GitHub] AutoHeal PR already open for {main_file}: {open_pr.html_url}")
        return {"pr_url": open_pr.html_url, "pr_number": open_pr.number, "branch": open_pr.head.ref,
                "base_branch": base_branch, "reused": True, "draft": getattr(open_pr, "draft", None),
                "labels": []}

    # 1. One commit with all files on top of the base branch (Git Data API), then a branch pointing to it.
    #    The branch only appears once the commit exists, so a failed commit leaves nothing behind.
    base_commit = repo.get_git_commit(repo.get_branch(base_branch).commit.sha)
    tree = repo.create_git_tree(
        [InputGitTreeElement(path, "100644", "blob", content=content) for path, content in files.items()],
        base_commit.tree,
    )
    message = f"AutoHeal: fix {main_file}"
    if len(files) > 1:
        message += f" (+{len(files) - 1} files)"
    commit = repo.create_git_commit(message, tree, [base_commit])
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch_name = f"{BRANCH_PREFIX}{timestamp}"
    branch_ref = repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=commit.sha)
    print(f"🌿 [GitHub] Committed {len(files)} file(s) on new branch {branch_name} from {base_branch}")

    try:
        body = build_pr_body(summary, root_cause_analysis, run_url, files, report)
        pr, draft = _open_pull_request(repo, main_file, body, branch_name, base_branch)
    except Exception:
        # Don't leave a branch without a PR behind
        try:
            branch_ref.delete()
            print(f"🧹 [GitHub] Deleted branch {branch_name} after a failure")
        except GithubException:
            pass
        raise

    labels = _add_bot_label(repo, pr)
    print(f"✅ [GitHub] {'Draft ' if draft else ''}Pull Request opened: {pr.html_url}")
    return {"pr_url": pr.html_url, "pr_number": pr.number, "branch": branch_name,
            "base_branch": base_branch, "reused": False, "draft": draft, "labels": labels}


def _open_pull_request(repo, main_file: str, body: str, branch_name: str, base_branch: str):
    """Open the PR as a Draft. Returns (pr, is_draft)."""
    kwargs = dict(title=f"{PR_TITLE_PREFIX}{main_file}", body=body, head=branch_name, base=base_branch)
    try:
        return repo.create_pull(**kwargs, draft=True), True
    except GithubException as e:
        # Private repos on the free plan can't have draft PRs; open a normal one and say so in the body
        if e.status == 422 and "draft" in str(e.data).lower():
            print("⚠️ [GitHub] Draft PRs are not available on this repo, opening a normal PR instead")
            kwargs["body"] = "> ⚠️ Draft PRs are not available on this repository. Treat this PR as a draft.\n\n" + body
            return repo.create_pull(**kwargs), False
        raise


def _ensure_label(repo, name: str, color: str, description: str) -> None:
    try:
        repo.get_label(name)
    except GithubException as e:
        if e.status != 404:
            raise
        repo.create_label(name, color, description)


def _add_bot_label(repo, pr) -> list[str]:
    """A missing label must not lose the PR, so failures here are only logged."""
    try:
        _ensure_label(repo, BOT_LABEL, "76b900", "Opened by AutoHeal Data Agent, needs human review")
        pr.add_to_labels(BOT_LABEL)
        return [BOT_LABEL]
    except GithubException as e:
        print(f"⚠️ [GitHub] Could not add label {BOT_LABEL}: {e}")
        return []


def _status_icon(ok) -> str:
    return "✅" if ok is True else ("❌" if ok is False else "⏭️")


REPRODUCED_TEXT = {True: "yes, the tests failed before the patch",
                   False: "no, the tests already passed before the patch",
                   None: "unknown"}


def build_pr_body(summary: str, root_cause_analysis: str = "", run_url: str | None = None,
                  files: dict[str, str] | None = None, report: dict | None = None) -> str:
    """
    report (all keys optional):
      iterations: int
      sast: {"passed", "layers": {layer: status}, "reasons": [...]}
      tests: {"passed", "skipped", "mode", "command", "reproduced", "before_output", "output"}
      impacted: [call sites / files affected]
    """
    report = report or {}
    parts = [
        "## 🤖 AutoHeal Data Agent",
        "This **Draft** Pull Request was opened automatically after a failed pipeline run. "
        "It must be reviewed by a human (CODEOWNERS) and is never merged by the bot.",
    ]
    if run_url:
        parts.append(f"**Failed run:** {run_url}")
    if report.get("iterations"):
        parts.append(f"**Iterations (Engineer → guardrails → sandbox):** {report['iterations']}")
    parts += ["### What changed", summary or "_No summary provided._"]

    if files:
        parts += ["### Files changed", "\n".join(f"- `{path}`" for path in files)]
    if report.get("impacted"):
        parts += ["### Affected call sites", "\n".join(f"- {site}" for site in report["impacted"])]

    sast = report.get("sast")
    if sast:
        rows = ["| Layer | Result |", "|---|---|"]
        rows += [f"| {layer} | {result} |" for layer, result in sast.get("layers", {}).items()]
        parts += [f"### {_status_icon(sast.get('passed'))} Static analysis (SAST)", "\n".join(rows)]

    tests = report.get("tests")
    if tests:
        if tests.get("skipped"):
            parts += ["### ⏭️ Sandbox tests", f"Not run: {tests.get('output', '')}"]
        else:
            parts += [
                f"### {_status_icon(tests.get('passed'))} Sandbox tests "
                f"(`{tests.get('command', '')}`, {tests.get('mode', '')})",
                f"Error reproduced: {REPRODUCED_TEXT[tests.get('reproduced')]}",
                f"<details><summary>Before the patch</summary>\n\n```\n{tests.get('before_output', '')}\n```\n</details>",
                f"<details><summary>After the patch</summary>\n\n```\n{tests.get('output', '')}\n```\n</details>",
            ]

    if root_cause_analysis:
        parts += ["<details><summary>Root Cause Analysis (Agent Researcher)</summary>\n",
                  root_cause_analysis, "\n</details>"]
    parts.append("\n_Please review before merging._")
    return "\n\n".join(parts)


def comment_on_pull_request(repo_full_name: str, pr_number: int, body: str) -> str:
    """Add a comment to a PR (used when the same error happens again). Returns the comment URL."""
    pr = _repo(repo_full_name).get_pull(pr_number)
    return pr.create_issue_comment(body).html_url


def create_escalation_issue(repo_full_name: str, title: str, body: str) -> dict:
    """Open an Issue for a human when the agent gives up. Returns {"issue_url", "issue_number"}."""
    repo = _repo(repo_full_name)
    labels = []
    for name, color, description in ((BOT_LABEL, "76b900", "Opened by AutoHeal Data Agent, needs human review"),
                                     (ESCALATION_LABEL, "d93f0b", "AutoHeal could not fix this automatically")):
        try:
            _ensure_label(repo, name, color, description)
            labels.append(name)
        except GithubException as e:
            print(f"⚠️ [GitHub] Could not create label {name}: {e}")
    issue = repo.create_issue(title=title, body=body, labels=labels)
    print(f"🆘 [GitHub] Escalation issue opened: {issue.html_url}")
    return {"issue_url": issue.html_url, "issue_number": issue.number}


def get_pull_request_status(repo_full_name: str, pr_number: int) -> dict:
    """Return the live state of a PR: "open", "closed" or "merged"."""
    pr = _repo(repo_full_name).get_pull(pr_number)
    state = "merged" if pr.merged else pr.state
    return {"state": state, "pr_url": pr.html_url, "draft": pr.draft}


# Quick connection test: python -m backend.github_integration owner/repo
if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DEMO_REPO", "")
    print(f"Auth mode: {auth_mode()}")
    if target:
        r = get_github_client(target).get_repo(target)
        print(f"Connected to {r.full_name} (default branch: {r.default_branch})")
