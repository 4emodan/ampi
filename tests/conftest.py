import sys
from pathlib import Path

import pytest

FAKE_CLAUDE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


@pytest.fixture
def fake_claude_env(tmp_path, monkeypatch):
    """Environment for the fake CLI; returns the path of its invocation log."""
    log = tmp_path / "claude_calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "echo")
    return log
