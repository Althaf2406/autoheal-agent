"""SAST pipeline (Sub-Task 12) and destructive operation policy (Sub-Task 13)."""
import pytest

from backend import guardrails as g
from conftest import FIXED, ORIGINAL

PATH = "etl/pipeline.py"


def check(new, old=ORIGINAL, path=PATH, **kwargs):
    diff = g.make_diff({path: old}, {path: new})
    return g.validate_patch(diff, {path: new}, {path: old}, **kwargs)


def test_minimal_fix_passes_every_layer():
    result = check(FIXED)
    assert result.passed and result.reasons == []
    assert result.layers["policy"] == result.layers["ast"] == result.layers["ruff"] == result.layers["bandit"] == "passed"


@pytest.mark.parametrize("snippet, rule", [
    ('conn.execute("DROP TABLE customers")', "drop"),
    ('conn.execute("drop table if exists customers")', "drop"),
    ('conn.execute("TRUNCATE TABLE sales")', "truncate"),
    ('conn.execute("DELETE FROM sales")', "delete-without-where"),
    ('conn.execute("ALTER TABLE sales DROP COLUMN price")', "alter-drop"),
    ('conn.execute(f"SELECT * FROM {table}")', "sql-fstring"),
    ('conn.execute("SELECT * FROM {}".format(table))', "sql-format"),
    ('conn.execute("SELECT * FROM t WHERE id = %s" % table)', "sql-percent"),
    ('conn.execute("SELECT * FROM t WHERE id = " + table)', "sql-concat"),
    ("os.remove(table)", "destructive-call"),
    ("shutil.rmtree(table)", "destructive-call"),
    ("Path(table).unlink()", "destructive-call"),
    ('os.system("rm -rf /data")', "rm-rf"),
])
def test_destructive_python_is_rejected(snippet, rule):
    new = FIXED + f"\n\ndef bad(conn, table):\n    {snippet}\n"
    result = check(new)
    assert not result.passed and result.layers["policy"] == "failed"
    assert any(f"policy {rule}:" in reason for reason in result.reasons), result.reasons
    assert not result.escalate  # fixable: goes back to the Engineer


def test_delete_with_where_and_parameterized_query_are_allowed():
    new = FIXED + '\n\ndef ok(conn, i):\n    conn.execute("DELETE FROM sales WHERE id = ?", (i,))\n'
    result = check(new)
    assert result.layers["policy"] == "passed", result.reasons


@pytest.mark.parametrize("sql, rule", [
    ("DROP TABLE customers;", "drop"),
    ("DELETE FROM orders;", "delete-without-where"),
    ("TRUNCATE orders;", "truncate"),
    ("ALTER TABLE orders\n  DROP COLUMN price;", "alter-drop"),
])
def test_destructive_sql_file_is_rejected(sql, rule):
    old = "SELECT price, quantity FROM orders;\n"
    result = check(old + sql + "\n", old=old, path="sql/report.sql")
    assert any(f"policy {rule}:" in r for r in result.reasons), result.reasons


def test_sql_comments_are_ignored():
    old = "SELECT 1;\n"
    result = check(old + "-- DROP TABLE customers (do not do this)\n", old=old, path="sql/report.sql")
    assert result.passed, result.reasons


def test_existing_violation_is_not_blamed_on_the_patch():
    old = ORIGINAL + '\n\ndef cleanup(path):\n    os.remove(path)\n'
    new = FIXED + '\n\ndef cleanup(path):\n    os.remove(path)\n'
    result = check(new, old=old)
    assert result.layers["policy"] == "passed", result.reasons


def test_ruff_finds_undefined_names():
    result = check(FIXED.replace("df[\"price\"]", "data[\"price\"]"))
    assert result.layers["ruff"] == "failed"
    assert any("ruff F821" in r and "data" in r for r in result.reasons)


def test_bandit_finds_eval_and_shell():
    new = FIXED + '\n\nimport subprocess\n\n\ndef run(cmd):\n    eval(cmd)\n    subprocess.call(cmd, shell=True)\n'
    result = check(new)
    assert result.layers["bandit"] == "failed"
    assert any("bandit B307" in r for r in result.reasons) and any("bandit B602" in r for r in result.reasons)


def test_syntax_error_skips_the_tools():
    result = check("def broken(:\n")
    assert result.layers["ast"] == "failed" and result.layers["ruff"] == "skipped: syntax error"
    assert any("SyntaxError" in r for r in result.reasons)


def test_diff_budget_escalates(monkeypatch):
    monkeypatch.setattr(g, "MAX_CHANGED_LINES", 3)
    result = check(FIXED + "".join(f"x{i} = {i}\n" for i in range(5)))
    assert not result.passed and result.escalate
    assert any("limit is 3" in r for r in result.reasons)


def test_too_many_files_escalates(monkeypatch):
    monkeypatch.setattr(g, "MAX_FILES", 1)
    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    result = g.validate_patch(g.make_diff({}, files), files, {})
    assert result.escalate and any("touches 2 files" in r for r in result.reasons)


def test_workflow_files_are_protected():
    files = {".github/workflows/etl.yml": "on: push\n"}
    result = g.validate_patch(g.make_diff({".github/workflows/etl.yml": "on: pull_request\n"}, files), files)
    assert result.escalate and any("protected" in r for r in result.reasons)


def test_impact_analysis_from_anggota_a():
    result = check(FIXED, check_impact=lambda old, new: ["main.py:12 transform(df) needs a new argument"])
    assert result.layers["impact"] == "failed"
    assert any("main.py:12" in r for r in result.reasons)


def test_missing_tool_is_skipped_not_failed(monkeypatch):
    monkeypatch.setitem(g.TOOLS, "ruff", (lambda: None, None))
    result = check(FIXED)
    assert result.passed and result.layers["ruff"] == "skipped: not installed"


def test_semgrep_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AUTOHEAL_SEMGREP", "0")
    assert check(FIXED).layers["semgrep"] == "skipped: not installed"


def test_diff_helpers():
    diff = g.make_diff({"a.py": "x = 1\n", "new.py": None}, {"a.py": "x = 2\n", "new.py": "y = 1"})
    assert g.diff_files(diff) == ["a.py", "new.py"]
    assert g.diff_stats(diff) == (2, 1)
    assert "\\ No newline at end of file" in diff
    assert g.make_diff({"a.py": "x\n"}, {"a.py": "x\n"}) == ""


def test_feedback_lists_reasons():
    text = g.format_feedback(check(FIXED + '\nimport os\nos.remove("x")\n'))
    assert "rejected" in text and "os.remove" in text
