from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from backend import github_integration as gi

FILE = "etl/pipeline.py"
REPORT = {
    "iterations": 2,
    "sast": {"passed": True, "layers": {"policy": "passed", "ruff": "passed", "bandit": "passed",
                                        "semgrep": "skipped: not installed"}},
    "tests": {"passed": True, "skipped": False, "mode": "docker", "command": "python -m pytest -q",
              "reproduced": True, "before_output": "KeyError: 'total_price'", "output": "3 passed"},
}


def fake_pr(number=7, branch="autoheal/fix-20261010-120000", title=f"{gi.PR_TITLE_PREFIX}{FILE}"):
    return SimpleNamespace(number=number, html_url=f"https://github.com/me/demo/pull/{number}",
                           title=title, head=SimpleNamespace(ref=branch), draft=True, add_to_labels=MagicMock())


@pytest.fixture
def repo(monkeypatch):
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_pulls.return_value = []
    repo.get_branch.return_value.commit.sha = "abc123"
    repo.create_git_commit.return_value.sha = "new-commit"
    repo.create_pull.return_value = fake_pr()
    client = MagicMock()
    client.get_repo.return_value = repo
    client.rate_limiting = (5000, 5000)
    monkeypatch.setattr(gi, "get_github_client", lambda name: client)
    return repo


def test_opens_draft_pull_request_with_label(repo):
    result = gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary", "rca", run_url="https://run",
                                        report=REPORT)

    tree_elements = repo.create_git_tree.call_args.args[0]
    assert [e._InputGitTreeElement__path for e in tree_elements] == [FILE]
    assert repo.create_git_commit.call_args.args[0] == f"AutoHeal: fix {FILE}"
    ref_kwargs = repo.create_git_ref.call_args.kwargs
    assert ref_kwargs["sha"] == "new-commit" and ref_kwargs["ref"].startswith("refs/heads/autoheal/fix-")

    pr_kwargs = repo.create_pull.call_args.kwargs
    assert pr_kwargs["draft"] is True and pr_kwargs["base"] == "main"
    assert pr_kwargs["title"] == f"{gi.PR_TITLE_PREFIX}{FILE}"
    repo.create_pull.return_value.add_to_labels.assert_called_once_with("autoheal-bot")
    assert result == {"pr_url": "https://github.com/me/demo/pull/7", "pr_number": 7,
                      "branch": ref_kwargs["ref"][len("refs/heads/"):], "base_branch": "main", "reused": False,
                      "draft": True, "labels": ["autoheal-bot"]}


def test_pr_body_has_everything_for_the_reviewer(repo):
    gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "Use price.", "The RCA", run_url="https://run",
                               report=REPORT)
    body = repo.create_pull.call_args.kwargs["body"]
    for expected in ("Draft", "CODEOWNERS", "never merged by the bot", "https://run", "Iterations", "2",
                     "Use price.", f"`{FILE}`", "Static analysis", "| bandit | passed |", "python -m pytest -q",
                     "Error reproduced: yes", "KeyError: 'total_price'", "3 passed", "The RCA"):
        assert expected in body, expected


def test_pr_body_when_sandbox_skipped():
    body = gi.build_pr_body("s", report={"tests": {"skipped": True, "output": "Sandbox is off."}})
    assert "Not run: Sandbox is off." in body


def test_multi_file_commit(repo):
    gi.create_fix_pull_request("me/demo", {FILE: "a\n", "main.py": "b\n"}, "summary")
    assert len(repo.create_git_tree.call_args.args[0]) == 2
    assert repo.create_git_commit.call_args.args[0] == f"AutoHeal: fix {FILE} (+1 files)"


def test_falls_back_to_normal_pr_when_draft_not_supported(repo):
    error = GithubException(422, {"message": "Draft pull requests are not supported in this repository."}, None)
    repo.create_pull.side_effect = [error, fake_pr()]
    result = gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    assert result["draft"] is False
    second = repo.create_pull.call_args_list[1].kwargs
    assert "draft" not in second and "Treat this PR as a draft" in second["body"]


def test_label_is_created_if_missing(repo):
    repo.get_label.side_effect = GithubException(404, "Not Found", None)
    gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    assert repo.create_label.call_args.args[0] == "autoheal-bot"


def test_label_failure_does_not_lose_the_pr(repo):
    repo.get_label.side_effect = GithubException(403, "Forbidden", None)
    result = gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    assert result["pr_number"] == 7 and result["labels"] == []


def test_reuses_open_autoheal_pr(repo):
    repo.get_pulls.return_value = [fake_pr(3, branch="feature/other", title="Unrelated"), fake_pr(5)]
    result = gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    assert result["pr_number"] == 5 and result["reused"] is True
    repo.create_git_ref.assert_not_called()


def test_deletes_branch_when_pr_fails(repo):
    repo.create_pull.side_effect = GithubException(422, "Validation Failed", None)
    with pytest.raises(GithubException):
        gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    repo.create_git_ref.return_value.delete.assert_called_once()


def test_commit_failure_creates_no_branch(repo):
    repo.create_git_commit.side_effect = GithubException(500, "boom", None)
    with pytest.raises(GithubException):
        gi.create_fix_pull_request("me/demo", {FILE: "fixed\n"}, "summary")
    repo.create_git_ref.assert_not_called()


def test_comment_and_escalation_issue(repo):
    repo.get_pull.return_value.create_issue_comment.return_value.html_url = "https://c"
    assert gi.comment_on_pull_request("me/demo", 7, "Terjadi lagi (x2)") == "https://c"
    repo.get_pull.return_value.create_issue_comment.assert_called_once_with("Terjadi lagi (x2)")

    repo.create_issue.return_value = SimpleNamespace(html_url="https://i", number=9)
    assert gi.create_escalation_issue("me/demo", "title", "body") == {"issue_url": "https://i", "issue_number": 9}
    assert repo.create_issue.call_args.kwargs["labels"] == ["autoheal-bot", "needs-human"]


def test_pr_status_includes_draft(repo):
    repo.get_pull.return_value = SimpleNamespace(merged=False, state="open", html_url="u", draft=True)
    assert gi.get_pull_request_status("me/demo", 7) == {"state": "open", "pr_url": "u", "draft": True}


def test_clone_url_with_token(monkeypatch):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_PRIVATE_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    assert gi.get_clone_url("me/demo") == "https://x-access-token:tok@github.com/me/demo.git"


def test_relative_key_path_resolves_from_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(gi, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "app.pem").write_text("KEY", encoding="utf-8")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "secrets/app.pem")
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    assert gi._load_private_key() == "KEY"
    assert gi.auth_mode() == "app"


def test_inline_key_with_escaped_newlines(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "line1\\nline2")
    assert gi._load_private_key() == "line1\nline2"


def test_no_credentials(monkeypatch):
    for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_PRIVATE_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(gi.GitHubConfigError):
        gi.get_github_client("me/demo")
    with pytest.raises(gi.GitHubConfigError):
        gi.get_clone_url("me/demo")


def test_non_numeric_app_id(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "my-app")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "KEY")
    with pytest.raises(gi.GitHubConfigError, match="must be a number"):
        gi.get_github_client("me/demo")
