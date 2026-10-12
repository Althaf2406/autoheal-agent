"""
Sandbox runner (Sub-Task 14, Anggota B).

Clones the failing repo at the failing commit, runs the tests BEFORE the patch (to check the error can be
reproduced), applies the patch and runs the tests again.

Modes (AUTOHEAL_SANDBOX):
  auto   - docker if the docker CLI is available, otherwise off (default)
  docker - throw-away container: --network none, read-only root filesystem (only /work and /tmp are
           writable), CPU/RAM/PID limits, no capabilities, 120 s timeout. Dependencies are installed once
           per requirements.txt in a separate step (with network) into a cache folder that is mounted
           read-only into the test container.
  local  - same steps directly on this machine, with a timeout and an environment without our secrets.
           NOT isolated: only for development on a machine without Docker.
  off    - nothing is run; the result is marked skipped.

Test command: pytest when the repo has tests, otherwise the failing script itself (`python <file>`),
which is how the demo repo's CI fails.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

from backend.contracts import TestResult

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "sandbox-cache")
TEST_TIMEOUT = int(os.environ.get("AUTOHEAL_SANDBOX_TIMEOUT", "120"))
INSTALL_TIMEOUT = 600
IMAGE = os.environ.get("AUTOHEAL_SANDBOX_IMAGE", "python:3.11-slim")
MAX_OUTPUT = 4000


def resolve_mode() -> str:
    mode = os.environ.get("AUTOHEAL_SANDBOX", "auto").strip().lower()
    if mode == "auto":
        return "docker" if shutil.which("docker") else "off"
    return mode if mode in ("docker", "local", "off") else "off"


def _scrub(text: str) -> str:
    """Never let a token from the clone URL end up in logs, the dashboard or a PR body."""
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text or "")


def _tail(text: str) -> str:
    text = _scrub(text)
    return text if len(text) <= MAX_OUTPUT else "...\n" + text[-MAX_OUTPUT:]


def _run(cmd: list[str], cwd: str | None = None, timeout: int = TEST_TIMEOUT, env: dict | None = None,
         stdin: str | None = None) -> tuple[int, str]:
    """Run a command, return (exit code, combined output). Exit code 124 means timeout."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, out + f"\n[sandbox] Timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def clone(repo_url: str, sha: str, workdir: str) -> None:
    for cmd in (["git", "init", "-q"],
                ["git", "fetch", "-q", "--depth", "1", repo_url, sha],
                ["git", "checkout", "-q", "FETCH_HEAD"]):
        code, out = _run(cmd, cwd=workdir, timeout=INSTALL_TIMEOUT)
        if code != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])} failed: {_scrub(out).strip()}")


def apply_patch(patch: str, workdir: str) -> str | None:
    """Apply a unified diff with git. Returns an error message, or None when it applied."""
    code, out = _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workdir, stdin=patch)
    return None if code == 0 else f"Patch could not be applied: {out.strip()}"


def test_command(workdir: str, file_path: str | None) -> list[str] | None:
    """pytest if the repo has tests, otherwise run the failing script. None if there is nothing to run."""
    for _root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", ".venv")]
        if any(re.match(r"(test_.*|.*_test)\.py$", f) for f in files):
            return ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if file_path and file_path.endswith(".py") and os.path.exists(os.path.join(workdir, file_path)):
        return ["python", file_path]
    return None


def _deps_dir(workdir: str) -> tuple[str, str]:
    """(cache folder for this requirements.txt, requirements text)."""
    req_path = os.path.join(workdir, "requirements.txt")
    requirements = ""
    if os.path.exists(req_path):
        with open(req_path, "r", encoding="utf-8") as f:
            requirements = f.read()
    key = hashlib.sha256(f"{IMAGE}|{sys.version_info[:2]}|{requirements}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, key), requirements


class DockerRunner:
    mode = "docker"

    def install(self, workdir: str, deps: str) -> tuple[int, str]:
        cmd = ["docker", "run", "--rm", "-v", f"{deps}:/deps", "-v", f"{workdir}:/work:ro", "-w", "/work",
               IMAGE, "sh", "-c",
               "pip install -q --no-cache-dir --target /deps pytest "
               "$( [ -f requirements.txt ] && echo '-r requirements.txt' )"]
        return _run(cmd, timeout=INSTALL_TIMEOUT)

    def command(self, workdir: str, deps: str, args: list[str], name: str) -> list[str]:
        return ["docker", "run", "--rm", "--name", name,
                "--network", "none",
                "--read-only", "--tmpfs", "/tmp:rw,size=256m",
                "--cpus", "1", "--memory", "1g", "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", f"{workdir}:/work:rw", "-v", f"{deps}:/deps:ro", "-w", "/work",
                "-e", "PYTHONPATH=/deps", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
                IMAGE, *args]

    def test(self, workdir: str, deps: str, args: list[str]) -> tuple[int, str]:
        name = f"autoheal-sandbox-{uuid.uuid4().hex[:8]}"
        code, out = _run(self.command(workdir, deps, args, name))
        if code == 124:
            _run(["docker", "kill", name], timeout=30)
        return code, out


class LocalRunner:
    """Development fallback. Runs on the host, so it is not a security boundary."""
    mode = "local"

    def install(self, workdir: str, deps: str) -> tuple[int, str]:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--target", deps, "pytest"]
        if os.path.exists(os.path.join(workdir, "requirements.txt")):
            cmd += ["-r", "requirements.txt"]
        return _run(cmd, cwd=workdir, timeout=INSTALL_TIMEOUT)

    def env(self, deps: str) -> dict:
        # Only what Python needs; API keys and tokens from our .env are NOT passed on
        keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "PATHEXT", "LANG")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(PYTHONPATH=deps, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        return env

    def test(self, workdir: str, deps: str, args: list[str]) -> tuple[int, str]:
        args = [sys.executable if args[0] == "python" else args[0], *args[1:]]
        return _run(args, cwd=workdir, env=self.env(deps))


RUNNERS = {"docker": DockerRunner, "local": LocalRunner}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_tests(repo_url: str, sha: str, patch: str, file_path: str | None = None) -> TestResult:
    """Contract function (Project Plan V2). `patch` is a unified diff (guardrails.make_diff)."""
    mode = resolve_mode()
    if mode == "off":
        return TestResult(passed=False, skipped=True, mode="off",
                          output="Sandbox is off (Docker not found or AUTOHEAL_SANDBOX=off); tests were not run.")
    runner = RUNNERS[mode]()
    workdir = tempfile.mkdtemp(prefix="autoheal-sandbox-")
    try:
        try:
            clone(repo_url, sha, workdir)
        except RuntimeError as e:
            return TestResult(passed=False, skipped=True, mode=mode, output=f"Sandbox setup failed: {e}")

        args = test_command(workdir, file_path)
        if args is None:
            return TestResult(passed=False, skipped=True, mode=mode, output="No tests or runnable script found.")

        deps, _ = _deps_dir(workdir)
        if not os.path.isdir(deps):
            os.makedirs(deps, exist_ok=True)
            code, out = runner.install(workdir, deps)
            if code != 0:
                shutil.rmtree(deps, ignore_errors=True)
                return TestResult(passed=False, skipped=True, mode=mode,
                                  output=f"Installing dependencies failed:\n{_tail(out)}")

        # Reproduce first: the unpatched code should fail
        before_code, before_out = runner.test(workdir, deps, args)

        problem = apply_patch(patch, workdir)
        if problem:
            return TestResult(passed=False, mode=mode, output=problem, reproduced=before_code != 0,
                              before_output=_tail(before_out), command=" ".join(args))

        after_code, after_out = runner.test(workdir, deps, args)
        return TestResult(passed=after_code == 0, output=_tail(after_out), mode=mode,
                          reproduced=before_code != 0, before_output=_tail(before_out), command=" ".join(args))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def format_feedback(result: TestResult) -> str:
    """Text sent back to the Agent Engineer when the tests fail after the patch."""
    return (f"Your patch was applied in a sandbox and `{result.command}` still fails. Output:\n"
            f"```\n{result.output}\n```\nFix the code so this command passes.")
