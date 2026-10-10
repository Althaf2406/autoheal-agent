from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from backend import github_integration as gi

FILE = "etl/pipeline.py"


def fake_pr(number=7, branch="autoheal/fix-20261010-120000", title=f"{gi.PR_TITLE_PREFIX}{FILE}"):
    return SimpleNamespace(number=number, html_url=f"https://github.com/me/demo/pull/{number}",
                           title=title, head=SimpleNamespace(ref=branch))


@pytest.fixture
def repo(monkeypatch):
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_pulls.return_value = []
    repo.get_branch.return_value.commit.sha = "abc123"
    repo.get_contents.return_value.sha = "file-sha"
    repo.create_pull.return_value = fake_pr()
    client = MagicMock()
    client.get_repo.return_value = repo
    monkeypatch.setattr(gi, "get_github_client", lambda name: client)
    return repo


def test_opens_pull_request(repo):
    result = gi.create_fix_pull_request("me/demo", FILE, "fixed\n", "summary", "rca", run_url="https://run")

    ref_kwargs = repo.create_git_ref.call_args.kwargs
    assert ref_kwargs["sha"] == "abc123" and ref_kwargs["ref"].startswith("refs/heads/autoheal/fix-")
    repo.update_file.assert_called_once()
    pr_kwargs = repo.create_pull.call_args.kwargs
    assert pr_kwargs["base"] == "main" and pr_kwargs["title"] == f"{gi.PR_TITLE_PREFIX}{FILE}"
    assert "https://run" in pr_kwargs["body"] and "rca" in pr_kwargs["body"]
    assert result["pr_number"] == 7 and result["reused"] is False


def test_creates_file_when_missing(repo):
    repo.get_contents.side_effect = GithubException(404, "Not Found", None)
    gi.create_fix_pull_request("me/demo", FILE, "fixed\n", "summary")
    repo.create_file.assert_called_once()
    repo.update_file.assert_not_called()


def test_reuses_open_autoheal_pr(repo):
    repo.get_pulls.return_value = [fake_pr(3, branch="feature/other", title="Unrelated"), fake_pr(5)]
    result = gi.create_fix_pull_request("me/demo", FILE, "fixed\n", "summary")
    assert result["pr_number"] == 5 and result["reused"] is True
    repo.create_git_ref.assert_not_called()


def test_deletes_branch_when_pr_fails(repo):
    repo.create_pull.side_effect = GithubException(422, "Validation Failed", None)
    with pytest.raises(GithubException):
        gi.create_fix_pull_request("me/demo", FILE, "fixed\n", "summary")
    repo.create_git_ref.return_value.delete.assert_called_once()


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


def test_non_numeric_app_id(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "my-app")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "KEY")
    with pytest.raises(gi.GitHubConfigError, match="must be a number"):
        gi.get_github_client("me/demo")
