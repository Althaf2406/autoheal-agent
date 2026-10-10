from types import SimpleNamespace

import pytest

from backend import agent_engineer

ORIGINAL = 'import pandas as pd\n\ndf = pd.read_csv("data/sales.csv")\ndf["revenue"] = df["total_price"] * df["quantity"]\n'
FIXED = 'import pandas as pd\n\ndf = pd.read_csv("data/sales.csv")\ndf["revenue"] = df["price"] * df["quantity"]\n'


def answer(code: str, summary: str = "Use price instead of total_price.") -> str:
    return f"SUMMARY:\n{summary}\n\nCODE:\n```python\n{code}```"


class FakeClient:
    """Returns the queued answers one by one and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        content, finish_reason = reply if isinstance(reply, tuple) else (reply, "stop")
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


@pytest.fixture
def use_client(monkeypatch):
    def install(*replies):
        client = FakeClient(*replies)
        monkeypatch.setattr(agent_engineer, "get_client", lambda: client)
        return client
    return install


def test_parse_strips_reasoning_before_answer():
    text = "<think>Maybe SUMMARY: wrong\n```python\nx = 1\n```</think>\n" + answer(FIXED)
    summary, code = agent_engineer.parse_engineer_response(text)
    assert summary == "Use price instead of total_price."
    assert code == FIXED


def test_parse_handles_missing_opening_think_tag():
    summary, code = agent_engineer.parse_engineer_response("thinking...\n</think>\n" + answer(FIXED))
    assert code == FIXED


def test_parse_without_code_block():
    assert agent_engineer.parse_engineer_response("SUMMARY: nothing")[1] is None


def test_detect_language():
    assert agent_engineer.detect_language("etl/load.sql") == ("SQL", "sql")
    assert agent_engineer.detect_language("etl/pipeline.py") == ("Python", "python")
    assert agent_engineer.detect_language(None) == ("Python", "python")


def test_validate_code():
    assert agent_engineer.validate_code(FIXED, "Python") is None
    assert "SyntaxError" in agent_engineer.validate_code("def broken(:\n", "Python")
    assert agent_engineer.validate_code("SELEC nonsense", "SQL") is None


def test_valid_fix_first_try(use_client):
    client = use_client(answer(FIXED))
    result = agent_engineer.run_engineer("KeyError: 'total_price'", "rca", "etl/pipeline.py", ORIGINAL)
    assert result == {"summary": "Use price instead of total_price.", "fixed_code": FIXED,
                      "language": "Python", "valid": True, "error": None}
    assert ORIGINAL in client.calls[0]["messages"][1]["content"]


def test_retries_after_syntax_error(use_client):
    client = use_client(answer("def broken(:\n"), answer(FIXED))
    result = agent_engineer.run_engineer("err", "rca", "etl/pipeline.py", ORIGINAL)
    assert result["valid"] and result["fixed_code"] == FIXED
    # The second request tells the model why the first answer was rejected
    assert "SyntaxError" in client.calls[1]["messages"][-1]["content"]


@pytest.mark.parametrize("bad_reply, expected_error", [
    (answer(ORIGINAL), "identical"),
    ("I think you should rename the column.", "did not contain a code block"),
    ((answer(FIXED), "length"), "cut off"),
])
def test_rejects_bad_answers(use_client, bad_reply, expected_error):
    use_client(bad_reply, bad_reply)
    result = agent_engineer.run_engineer("err", "rca", "etl/pipeline.py", ORIGINAL)
    assert not result["valid"]
    assert expected_error in result["error"]


def test_missing_api_key(monkeypatch):
    monkeypatch.setattr(agent_engineer, "_client", None)
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NEBIUS_API_KEY"):
        agent_engineer.get_client()
