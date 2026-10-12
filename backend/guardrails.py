"""
Guardrails for patches written by the Agent Engineer (Sub-Task 12 & 13, Anggota B).

A patch must pass every layer before it can be tested in the sandbox:
  1. policy  - diff budget, protected paths and destructive operations (DROP, TRUNCATE, DELETE without
               WHERE, ALTER ... DROP, os.remove, shutil.rmtree, rm -rf, SQL built with f-strings/format/%)
  2. ast     - Python syntax
  3. ruff    - fast lint: syntax errors and undefined names
  4. bandit  - Python security issues (eval, subprocess with shell=True, hardcoded secrets, SQL injection)
  5. semgrep - extra rules in backend/semgrep_rules.yml (optional: skipped when semgrep is not installed)
  6. impact  - check_impact() from Anggota A: call sites broken by a changed function signature

Only problems the patch ADDS are reported. Findings that already exist in the original file are ignored,
otherwise a file that already used os.remove could never be fixed.
"""
from __future__ import annotations

import ast
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from typing import Callable

from backend.contracts import ValidationResult

MAX_CHANGED_LINES = int(os.environ.get("AUTOHEAL_MAX_DIFF_LINES", "80"))
MAX_FILES = int(os.environ.get("AUTOHEAL_MAX_FILES", "3"))
PROTECTED_PATHS = (".github/workflows/", ".github/CODEOWNERS", "CODEOWNERS")
TOOL_TIMEOUT = 60
SEMGREP_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semgrep_rules.yml")

# Hard errors only (same set flake8 recommends for CI): syntax errors and undefined names
RUFF_RULES = "E9,F63,F7,F82"

SQL_DESTRUCTIVE = [
    ("drop", re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW|INDEX)\b", re.I)),
    ("truncate", re.compile(r"\bTRUNCATE\s+(TABLE\s+)?[\w.\"`\[]", re.I)),
    ("alter-drop", re.compile(r"\bALTER\s+TABLE\s+\S+\s+DROP\b", re.I)),
]
DELETE_FROM = re.compile(r"\bDELETE\s+FROM\b", re.I)
WHERE = re.compile(r"\bWHERE\b", re.I)
RM_RF = re.compile(r"\brm\s+-(\w*r\w*f|\w*f\w*r)\w*\b")
# Looks like a SQL statement (used to spot SQL built from variables)
SQL_STATEMENT = re.compile(
    r"\b(SELECT\s.+?\sFROM|INSERT\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM|DROP\s+TABLE|CREATE\s+TABLE|"
    r"ALTER\s+TABLE|TRUNCATE)\b", re.I | re.S)
DESTRUCTIVE_CALLS = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "shutil.rmtree",
                     "remove", "unlink", "rmdir", "removedirs", "rmtree"}


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def make_diff(old_files: dict[str, str | None], new_files: dict[str, str]) -> str:
    """Unified diff (git style, a/ and b/ prefixes) that `git apply` accepts."""
    parts = []
    for path, new in new_files.items():
        old = old_files.get(path)
        old_lines = (old or "").splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        if old_lines == new_lines:
            continue
        fromfile = f"a/{path}" if old is not None else "/dev/null"
        lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=f"b/{path}"))
        out = []
        for line in lines:
            if not line.endswith("\n"):
                # git needs this marker when the last line has no newline
                line += "\n\\ No newline at end of file\n"
            out.append(line)
        parts.append(f"diff --git a/{path} b/{path}\n" + "".join(out))
    return "".join(parts)


def diff_files(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path != "/dev/null" and path not in files:
                files.append(path)
    return files


def diff_stats(diff: str) -> tuple[int, int]:
    """(added, removed) lines, headers excluded."""
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


# ---------------------------------------------------------------------------
# Layer 1: destructive operation policy (Sub-Task 13)
# ---------------------------------------------------------------------------

def _sql_findings(sql: str) -> list[tuple[str, str]]:
    findings = []
    for statement in sql.split(";"):
        compact = " ".join(statement.split())
        if not compact:
            continue
        for rule, pattern in SQL_DESTRUCTIVE:
            if pattern.search(compact):
                findings.append((rule, compact[:120]))
        if DELETE_FROM.search(compact) and not WHERE.search(compact):
            findings.append(("delete-without-where", compact[:120]))
    return findings


def _sql_comment_free(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _python_policy_findings(source: str) -> list[tuple[str, str]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []  # reported by the ast layer
    findings = []
    for node in ast.walk(tree):
        # Plain strings: destructive SQL or shell commands written as text
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            findings += _sql_findings(node.value)
            if RM_RF.search(node.value):
                findings.append(("rm-rf", node.value[:120]))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            findings += _sql_findings(text)
            if RM_RF.search(text):
                findings.append(("rm-rf", text[:120]))
            if any(isinstance(v, ast.FormattedValue) for v in node.values) and SQL_STATEMENT.search(text):
                findings.append(("sql-fstring", ast.unparse(node)[:120]))
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            short = name.rsplit(".", 1)[-1]
            if name in DESTRUCTIVE_CALLS or (short in ("unlink", "rmdir", "rmtree") and "." in name):
                findings.append(("destructive-call", ast.unparse(node)[:120]))
            # "SELECT ... {}".format(x)
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "format"
                    and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str)
                    and SQL_STATEMENT.search(node.func.value.value)):
                findings.append(("sql-format", ast.unparse(node)[:120]))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            # "SELECT ... %s" % x   or   "SELECT ... " + x
            for side, other in ((node.left, node.right), (node.right, node.left)):
                if (isinstance(side, ast.Constant) and isinstance(side.value, str)
                        and SQL_STATEMENT.search(side.value) and not isinstance(other, ast.Constant)):
                    rule = "sql-percent" if isinstance(node.op, ast.Mod) else "sql-concat"
                    findings.append((rule, ast.unparse(node)[:120]))
                    break
    return findings


def policy_findings(path: str, source: str | None) -> list[tuple[str, str]]:
    if not source:
        return []
    if path.lower().endswith(".sql"):
        return _sql_findings(_sql_comment_free(source))
    if path.lower().endswith(".py"):
        return _python_policy_findings(source)
    findings = _sql_findings(source)
    findings += [("rm-rf", line.strip()[:120]) for line in source.splitlines() if RM_RF.search(line)]
    return findings


def _new_findings(new: list[tuple[str, str]], old: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Findings in `new` that are not already in `old` (compared as a multiset, so line shifts don't matter)."""
    remaining = Counter(old)
    added = []
    for finding in new:
        if remaining[finding]:
            remaining[finding] -= 1
        else:
            added.append(finding)
    return added


# ---------------------------------------------------------------------------
# Layers 3-5: external tools
# ---------------------------------------------------------------------------

def _python_module_cmd(module: str) -> list[str] | None:
    if importlib.util.find_spec(module) is not None:
        return [sys.executable, "-m", module]
    exe = shutil.which(module)
    return [exe] if exe else None


def _semgrep_cmd() -> list[str] | None:
    if os.environ.get("AUTOHEAL_SEMGREP", "1") == "0":
        return None
    exe = shutil.which("semgrep")
    return [exe] if exe else None


def _line_at(files: dict[str, str], path: str, lineno: int) -> str:
    lines = files.get(path, "").splitlines()
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


def _write_tree(root: str, files: dict[str, str | None]) -> None:
    for path, content in files.items():
        if content is None:
            continue
        full = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(content)


def _rel(root: str, filename: str) -> str:
    return os.path.relpath(os.path.join(root, filename), root).replace(os.sep, "/")


def _run_tool(cmd: list[str], cwd: str) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=TOOL_TIMEOUT,
                          encoding="utf-8", errors="replace")
    return proc.stdout


def _ruff(root: str, files: dict[str, str]) -> list[tuple[str, str]]:
    cmd = _python_module_cmd("ruff")
    out = _run_tool(cmd + ["check", "--isolated", "--no-cache", "--select", RUFF_RULES,
                           "--output-format", "json", "--exit-zero", "."], root)
    findings = []
    for item in json.loads(out or "[]"):
        path = _rel(root, item["filename"])
        line = _line_at(files, path, item["location"]["row"])
        findings.append((f"ruff {item['code']}", f"{path}: {item['message']} | {line}"))
    return findings


def _bandit(root: str, files: dict[str, str]) -> list[tuple[str, str]]:
    cmd = _python_module_cmd("bandit")
    out = _run_tool(cmd + ["-r", ".", "-f", "json", "-q", "--severity-level", "medium"], root)
    findings = []
    for item in json.loads(out or "{}").get("results", []):
        path = _rel(root, item["filename"])
        line = _line_at(files, path, item["line_number"])
        findings.append((f"bandit {item['test_id']}", f"{path}: {item['issue_text']} | {line}"))
    return findings


def _semgrep(root: str, files: dict[str, str]) -> list[tuple[str, str]]:
    cmd = _semgrep_cmd()
    out = _run_tool(cmd + ["--config", SEMGREP_RULES, "--json", "--metrics", "off", "--quiet", "."], root)
    findings = []
    for item in json.loads(out or "{}").get("results", []):
        path = _rel(root, item["path"])
        line = _line_at(files, path, item["start"]["line"])
        findings.append((f"semgrep {item['check_id'].rsplit('.', 1)[-1]}", f"{path}: {line}"))
    return findings


TOOLS = {
    "ruff": (lambda: _python_module_cmd("ruff"), _ruff),
    "bandit": (lambda: _python_module_cmd("bandit"), _bandit),
    "semgrep": (_semgrep_cmd, _semgrep),
}


def _run_tool_layer(name: str, old_files: dict[str, str | None], new_files: dict[str, str]):
    """Return (status, new findings) for one external tool."""
    find_cmd, runner = TOOLS[name]
    if not find_cmd():
        return "skipped: not installed", []
    py_new = {p: c for p, c in new_files.items() if p.endswith(".py")}
    if not py_new:
        return "skipped: no Python files", []
    py_old = {p: old_files.get(p) for p in py_new}
    try:
        with tempfile.TemporaryDirectory() as old_root, tempfile.TemporaryDirectory() as new_root:
            _write_tree(old_root, py_old)
            _write_tree(new_root, py_new)
            before = runner(old_root, {p: c for p, c in py_old.items() if c is not None})
            after = runner(new_root, py_new)
    except subprocess.TimeoutExpired:
        return f"skipped: timed out after {TOOL_TIMEOUT}s", []
    except (OSError, ValueError, KeyError) as e:
        return f"skipped: {type(e).__name__}: {e}", []
    found = _new_findings(after, before)
    return ("failed" if found else "passed"), found


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_patch(diff: str, new_files: dict[str, str] | None = None,
                   old_files: dict[str, str | None] | None = None,
                   check_impact: Callable[[str, str], list[str]] | None = None) -> ValidationResult:
    """
    Check a patch before it is tested or sent as a PR.

    `diff` is the unified diff (see make_diff). `new_files`/`old_files` map path -> full content and are
    needed for the content checks; with only a diff, just the budget and protected-path rules run.
    `check_impact` is Anggota A's impact analysis (old_src, new_src) -> broken call sites.
    """
    new_files = new_files or {}
    old_files = old_files or {}
    reasons: list[str] = []
    layers: dict[str, str] = {}
    escalate = False

    # --- 1. Policy: budget and protected paths (hard rules -> escalate to a human) ---
    touched = diff_files(diff) or list(new_files)
    added, removed = diff_stats(diff)
    if added + removed > MAX_CHANGED_LINES:
        reasons.append(f"policy: patch changes {added + removed} lines, the limit is {MAX_CHANGED_LINES}.")
        escalate = True
    if len(touched) > MAX_FILES:
        reasons.append(f"policy: patch touches {len(touched)} files, the limit is {MAX_FILES}.")
        escalate = True
    for path in touched:
        if any(path == p or path.startswith(p) for p in PROTECTED_PATHS):
            reasons.append(f"policy: {path} is protected and must not be changed by the bot.")
            escalate = True

    # --- 1b. Policy: destructive operations added by the patch (fixable -> feedback) ---
    for path, new in new_files.items():
        for rule, snippet in _new_findings(policy_findings(path, new), policy_findings(path, old_files.get(path))):
            reasons.append(f"policy {rule}: {path}: {snippet}")
    layers["policy"] = "failed" if reasons else "passed"

    # --- 2. Python syntax ---
    syntax_errors = []
    for path, new in new_files.items():
        if path.endswith(".py"):
            try:
                ast.parse(new)
            except SyntaxError as e:
                syntax_errors.append(f"ast: {path}: SyntaxError on line {e.lineno}: {e.msg}")
    layers["ast"] = "failed" if syntax_errors else "passed"
    reasons += syntax_errors

    # --- 3-5. ruff, bandit, semgrep (only when the code parses, otherwise they just repeat the error) ---
    for name in TOOLS:
        if syntax_errors:
            layers[name] = "skipped: syntax error"
            continue
        status, found = _run_tool_layer(name, old_files, new_files)
        layers[name] = status
        reasons += [f"{rule}: {detail}" for rule, detail in found]

    # --- 6. Impact analysis (Anggota A) ---
    if check_impact and not syntax_errors:
        broken = []
        for path, new in new_files.items():
            if old_files.get(path) is not None and path.endswith(".py"):
                broken += check_impact(old_files[path], new)
        layers["impact"] = "failed" if broken else "passed"
        reasons += [f"impact: call site not updated: {site}" for site in broken]

    return ValidationResult(passed=not reasons, reasons=reasons, escalate=escalate, layers=layers)


def format_feedback(result: ValidationResult) -> str:
    """Text sent back to the Agent Engineer when the patch is rejected."""
    lines = "\n".join(f"- {reason}" for reason in result.reasons)
    return f"The security guardrails rejected your patch:\n{lines}\nRemove these problems and keep the fix minimal."
