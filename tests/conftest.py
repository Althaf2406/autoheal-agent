import os
import sys

import pytest

# The Agent Researcher creates its LLM client on import, so it needs some key to exist
os.environ.setdefault("NEBIUS_API_KEY", "test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import run_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Keep tests away from the real data/runs.json and from secrets in a local .env."""
    monkeypatch.setattr(run_store, "STORE_PATH", str(tmp_path / "runs.json"))
    monkeypatch.setattr(run_store, "_runs", {})
    monkeypatch.delenv("AUTOHEAL_WEBHOOK_SECRET", raising=False)
