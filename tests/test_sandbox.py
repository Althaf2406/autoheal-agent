"""Sandbox runner (Sub-Task 14). The local mode runs for real against a throw-away git repo; Docker is checked
by the command it builds, since Docker is not needed to run the tests."""
import os
import subprocess

import pytest

from backend import guardrails, sandbox

BROKEN = ('import csv\n\nrows = list(csv.DictReader(open("data.csv")))\n'
          'print(sum(float(r["total_price"]) for r in rows))\n')
FIXED = ('import csv\n\nrows = list(csv.DictReader(open("data.csv")))\n'
         'print(sum(float(r["price"]) * int(r["quantity"]) for r in rows))\n')
TEST_FILE = ('import subprocess, sys\n\n\ndef test_pipeline_runs():\n'
             '    assert subprocess.run([sys.executable, "pipeline.py"]).returncode == 0\n')


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path):
    """A local git repo standing in for GitHub. Returns (url, sha)."""
    repo = tmp_path / "origin"
    repo.mkdir()
    (repo / "pipeline.py").write_text(BROKEN, encoding="utf-8")
    (repo / "data.csv").write_text("price,quantity\n10,2\n5,1\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo.as_uri(), sha, repo


@pytest.fixture
def local_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOHEAL_SANDBOX", "local")
    monkeypatch.setattr(sandbox, "CACHE_DIR", str(tmp_path / "cache"))
    # Nothing to install for these repos (pytest comes from the test environment itself)
    monkeypatch.setattr(sandbox.LocalRunner, "install", lambda self, workdir, deps: (0, ""))


def patch_for(new):
    return guardrails.make_diff({"pipeline.py": BROKEN}, {"pipeline.py": new})


def test_mode_resolution(monkeypatch):
    monkeypatch.setenv("AUTOHEAL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    assert sandbox.resolve_mode() == "off"
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    assert sandbox.resolve_mode() == "docker"
    monkeypatch.setenv("AUTOHEAL_SANDBOX", "nonsense")
    assert sandbox.resolve_mode() == "off"


def test_off_mode_is_skipped():
    result = sandbox.run_tests("https://example.invalid/repo.git", "abc", "")
    assert result.skipped and not result.passed and result.mode == "off"


def test_reproduces_then_passes_with_script(origin, local_mode):
    url, sha, _ = origin
    result = sandbox.run_tests(url, sha, patch_for(FIXED), file_path="pipeline.py")
    assert result.mode == "local" and result.command.endswith("pipeline.py")
    assert result.reproduced is True and "KeyError" in result.before_output
    assert result.passed, result.output
    assert "25.0" in result.output


def test_bad_patch_fails_tests(origin, local_mode):
    url, sha, _ = origin
    result = sandbox.run_tests(url, sha, patch_for(FIXED.replace('"price"', '"unit_price"')), file_path="pipeline.py")
    assert not result.passed and not result.skipped and "KeyError" in result.output
    assert "still fails" in sandbox.format_feedback(result)


def test_uses_pytest_when_repo_has_tests(origin, local_mode):
    url, _, repo = origin
    (repo / "tests").mkdir()
    (repo / "tests" / "test_pipeline.py").write_text(TEST_FILE, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tests")
    result = sandbox.run_tests(url, "HEAD", patch_for(FIXED), file_path="pipeline.py")
    assert "pytest" in result.command
    assert result.reproduced and result.passed, result.output


def test_patch_that_does_not_apply(origin, local_mode):
    url, sha, _ = origin
    wrong = guardrails.make_diff({"pipeline.py": "something else\n"}, {"pipeline.py": FIXED})
    result = sandbox.run_tests(url, sha, wrong, file_path="pipeline.py")
    assert not result.passed and "could not be applied" in result.output


def test_clone_failure_is_skipped_and_token_hidden(local_mode, tmp_path):
    url = "https://x-access-token:SUPERSECRET@127.0.0.1:9/me/demo.git"
    result = sandbox.run_tests(url, "abc", "", file_path="pipeline.py")
    assert result.skipped and "setup failed" in result.output
    assert "SUPERSECRET" not in result.output


def test_local_env_has_no_secrets(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    env = sandbox.LocalRunner().env("/deps")
    assert "NEBIUS_API_KEY" not in env and "GITHUB_TOKEN" not in env and env["PYTHONPATH"] == "/deps"


def test_docker_command_is_isolated():
    cmd = sandbox.DockerRunner().command("/tmp/work", "/tmp/deps", ["python", "-m", "pytest"], "box")
    joined = " ".join(cmd)
    for flag in ("--network none", "--read-only", "--memory", "--cpus", "--pids-limit", "--cap-drop ALL",
                 "no-new-privileges", "/tmp/deps:/deps:ro"):
        assert flag in joined
    assert cmd[-3:] == ["python", "-m", "pytest"]


def test_docker_timeout_kills_container(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return (124, "timed out") if cmd[1] == "run" else (0, "")
    monkeypatch.setattr(sandbox, "_run", fake_run)
    code, _ = sandbox.DockerRunner().test("/w", "/d", ["python", "x.py"])
    assert code == 124 and calls[-1][:2] == ["docker", "kill"]


def test_timeout_returns_124():
    code, out = sandbox._run(["python", "-c", "import time; time.sleep(5)"], timeout=1)
    assert code == 124 and "Timed out" in out


def test_nothing_to_run(tmp_path):
    assert sandbox.test_command(str(tmp_path), "etl/missing.py") is None
    os.makedirs(tmp_path / "etl")
    (tmp_path / "etl" / "job.py").write_text("print(1)\n")
    assert sandbox.test_command(str(tmp_path), "etl/job.py") == ["python", "etl/job.py"]
