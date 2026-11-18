import asyncio
import json
import os

from conftest import FAKE_CLAUDE

from assistant_bridge.claude_runner import ClaudeRunner, build_args, clean_env, parse_output


def runner(timeout=30):
    return ClaudeRunner(FAKE_CLAUDE, timeout_seconds=timeout, max_concurrent=2, env=clean_env(os.environ, []))


def turn(r, tmp_path, started=False, **kw):
    return asyncio.run(
        r.run_turn(
            kw.pop("prompt", "hello"),
            cwd=tmp_path,
            session_id="11111111-1111-1111-1111-111111111111",
            started=started,
            model="sonnet",
            allowed_tools=["Read", "WebSearch"],
            append_system_prompt="sys",
            **kw,
        )
    )


def calls(log):
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_build_args_new_and_resume():
    new = build_args(["claude"], session_id="s", resume=False, model=None, allowed_tools=["Read"], append_system_prompt=None)
    assert new == ["claude", "-p", "--output-format", "json", "--session-id", "s", "--allowedTools", "Read"]
    resumed = build_args(["claude"], session_id="s", resume=True, model="opus", allowed_tools=[], append_system_prompt="x")
    assert resumed[4:6] == ["--resume", "s"]
    assert "--bare" not in resumed and ["--model", "opus"] == resumed[6:8]


def test_clean_env_drops_tokens_and_billing_vars():
    env = clean_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "TG_A": "t"}, ["TG_A"])
    assert env == {"PATH": "/bin"}


def test_parse_output_variants():
    ok = parse_output(0, 'noise\n{"type":"result","subtype":"success","is_error":false,"result":"hi","total_cost_usd":0.1}', "")
    assert ok.ok and ok.text == "hi" and ok.cost_usd == 0.1
    bad = parse_output(1, "", "boom")
    assert not bad.ok and bad.error == "boom"


def test_run_turn_passes_prompt_on_stdin_and_runs_in_workspace(tmp_path, fake_claude_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    r = ClaudeRunner(FAKE_CLAUDE, timeout_seconds=30, max_concurrent=1, env=clean_env(os.environ, []))
    result = turn(r, tmp_path, prompt="-dash prompt")
    assert result.ok
    assert result.text == "echo: -dash prompt"
    (call,) = calls(fake_claude_env)
    assert call["prompt"] == "-dash prompt"
    assert "--session-id" in call["args"]
    assert os.path.samefile(call["cwd"], tmp_path)
    assert call["env_has_api_key"] is False


def test_missing_session_falls_back_to_new_session(tmp_path, fake_claude_env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "missing")
    result = turn(runner(), tmp_path, started=True)
    assert result.ok
    first, second = calls(fake_claude_env)
    assert "--resume" in first["args"] and "--session-id" in second["args"]


def test_session_in_use_falls_back_to_resume(tmp_path, fake_claude_env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "inuse")
    result = turn(runner(), tmp_path, started=False)
    assert result.ok
    first, second = calls(fake_claude_env)
    assert "--session-id" in first["args"] and "--resume" in second["args"]


def test_error_result_and_garbage_output(tmp_path, fake_claude_env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    result = turn(runner(), tmp_path)
    assert not result.ok and "overloaded" in result.error
    assert len(calls(fake_claude_env)) == 1  # no pointless retry

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    result = turn(runner(), tmp_path)
    assert not result.ok and "something went wrong" in result.error


def test_timeout_stops_the_process(tmp_path, fake_claude_env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    result = turn(runner(timeout=1), tmp_path)
    assert not result.ok and result.timed_out
