"""Runs that handle outside content (watchers, groups) are marked, and so are the approvals they ask for."""

import asyncio
import json
import os

from conftest import FAKE_CLAUDE
from test_youtube import FakeAuth, ask, yt  # noqa: F401 (yt is a fixture)

from ampi_tools import client as cli
from assistant_bridge.claude_runner import ClaudeRunner, clean_env


def test_the_runner_marks_runs_with_their_origin(tmp_path, fake_claude_env):
    runner = ClaudeRunner(FAKE_CLAUDE, timeout_seconds=30, max_concurrent=1, env=clean_env(os.environ, []))
    for origin in ("watcher:gmail-inbox", None):
        asyncio.run(runner.run_turn("hi", cwd=tmp_path, session_id="00000000-0000-0000-0000-000000000001",
                                    started=False, model=None, allowed_tools=["Read"], origin=origin))
    calls = [json.loads(line) for line in fake_claude_env.read_text().splitlines()]
    assert [c["origin"] for c in calls] == ["watcher:gmail-inbox", None]


def test_the_cli_forwards_the_origin(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "request", lambda payload: sent.append(payload) or {"ok": True, "text": "x"})
    monkeypatch.setenv("AMPI_ORIGIN", "watcher:yt")
    cli.youtube_main(["subscriptions"])
    monkeypatch.delenv("AMPI_ORIGIN")
    cli.youtube_main(["subscriptions"])
    assert [p["origin"] for p in sent] == ["watcher:yt", None]


def test_approvals_asked_for_by_watchers_say_so(yt):
    daemon, server, _ = yt(auth=FakeAuth())
    ops = [{"op": "youtube.subscribe", "args": {"channel": "@mkbhd"}}]
    resp = asyncio.run(daemon.handle({"cmd": "run", "ops": ops, "origin": "watcher:yt-uploads"}, 1))
    assert resp["ok"] and "Asked for by watcher:yt-uploads while handling outside content" in resp["text"]
    resp = ask(daemon, ("youtube.subscribe", {"channel": "@mkbhd"}))
    assert "Asked for by" not in resp["text"]
