import pytest

from ampi_tools import watch_spec as ws

PASCAL = b'''
name = "pascal-school-mail"
description = "Summary of every email from Pascal school"

[[triggers]]
type = "gmail.new"
query = "in:inbox"

[[steps]]
id = "from_pascal"
type = "script"
run = "lib/match_sender"
args = { patterns = ["pascal"], list = "pascal-addresses.txt" }

[[steps]]
id = "mail"
type = "adapter"
op = "gmail.show"
args = { ids = ["{{item.id}}"] }

[[steps]]
id = "summary"
type = "llm"
model = "haiku"
prompt = "prompts/summary.md"
input = "{{steps.mail.text}}"
schema = { outcome = ["info", "action_needed", "urgent"], summary = "text:700", due = "text:40?" }

[[steps]]
id = "send"
type = "notify"
template = "templates/summary.txt"
'''


def files(toml=PASCAL, **extra):
    f = {"watcher.toml": toml, "lib/match_sender.py": b"print()", "prompts/summary.md": b"Summarize.",
         "templates/summary.txt": b"{{item.subject}}\n{{steps.summary.summary}}"}
    f.update({k.replace("__", "/"): v for k, v in extra.items()})
    return f


def test_pascal_is_low_risk():
    spec = ws.parse(files())
    entries = ws.manifest(spec)
    assert ws.risk(entries)[0] == "low"
    assert ws.grant_list(entries) == []
    text = ws.describe(spec, entries)
    assert "🟢 low" in text and "new email (in:inbox)" in text


def test_hash_changes_with_any_file():
    a = ws.snapshot_hash(files())
    b = ws.snapshot_hash(files(**{"prompts__summary.md": b"Summarize!"}))
    assert a != b


def test_unused_file_refused():
    with pytest.raises(ws.SpecError, match="not used"):
        ws.parse(files(**{"scripts__evil.py": b"x"}))


def test_bad_paths_refused():
    with pytest.raises(ws.SpecError):
        ws.check_path("../x")
    with pytest.raises(ws.SpecError):
        ws.check_path("/etc/passwd")
    with pytest.raises(ws.SpecError):
        ws.check_snapshot({"watcher.toml": b"", "other/x.py": b""})


def test_reference_to_later_step_refused():
    bad = PASCAL.replace(b'input = "{{steps.mail.text}}"', b'input = "{{steps.send.text}}"')
    with pytest.raises(ws.SpecError, match="doesn't come before"):
        ws.parse(files(bad))


def test_render_is_single_pass_and_keeps_types():
    ctx = {"item": {"subject": "{{steps.secret.value}}", "ids": ["a", "b"]}, "steps": {"secret": {"value": "X"}}}
    assert ws.render("S: {{item.subject}}", ctx) == "S: {{steps.secret.value}}"
    assert ws.render("{{item.ids}}", ctx) == ["a", "b"]
    assert ws.render({"{{item.subject}}": 1}, ctx) == {"{{item.subject}}": 1}


def test_check_output():
    schema = ws.check_schema({"outcome": ["a", "b"], "summary": "text:5", "n": "int?",
                              "tags": "list[text:3]:2"}, "s")
    out = ws.check_output(schema, {"outcome": "a", "summary": "abcdefgh", "tags": ["xxxx", "y", "z"], "extra": 1})
    assert out == {"outcome": "a", "summary": "abcde", "n": None, "tags": ["xxx", "y"]}
    with pytest.raises(ws.SpecError):
        ws.check_output(schema, {"outcome": "c", "summary": "x", "tags": []})


def test_variable_route_is_high_fixed_is_medium():
    base = PASCAL + b'''
[[steps]]
id = "fwd"
type = "adapter"
op = "telegram.send"
args = { chat = "CHAT", text = "{{steps.summary.summary}}" }
'''
    spec = ws.parse(files(base.replace(b"CHAT", b"123")))
    assert ws.risk(ws.manifest(spec))[0] == "medium"
    spec = ws.parse(files(base.replace(b"CHAT", b"{{item.from}}")))
    level, reasons = ws.risk(ws.manifest(spec))
    assert level == "high" and any("run time" in r for r in reasons)


def test_script_actions_undeclared_route_is_variable():
    toml = PASCAL.replace(b'args = { patterns = ["pascal"], list = "pascal-addresses.txt" }',
                          b'args = {}\nactions = [{ op = "todoist.add" }]')
    entries = ws.manifest(ws.parse(files(toml)))
    assert ws.risk(entries)[0] == "high"
    toml = PASCAL.replace(b'args = { patterns = ["pascal"], list = "pascal-addresses.txt" }',
                          b'args = {}\nactions = [{ op = "todoist.add", project = ["Home", "School"], '
                          b'section = "x", parent = "y" }]')
    entries = ws.manifest(ws.parse(files(toml)))
    assert ws.risk(entries)[0] == "medium"
    assert ws.route_allowed({"project": ["Home", "School"]}, {"project": "Home"})
    assert not ws.route_allowed({"project": ["Home", "School"]}, {"project": "Work"})


def test_agent_tools_checked():
    toml = PASCAL.replace(b'schema = { outcome = ["info", "action_needed", "urgent"], summary = "text:700", '
                          b'due = "text:40?" }', b'mode = "agent"\ntools = ["Bash(python3:*)"]')
    with pytest.raises(ws.SpecError, match="run anything"):
        ws.parse(files(toml))
    toml2 = toml.replace(b"Bash(python3:*)", b"Bash(todoist:*)")
    level, _ = ws.risk(ws.manifest(ws.parse(files(toml2))))
    assert level == "high"


def test_not_for_watchers_and_templated_op():
    bad = PASCAL.replace(b'op = "gmail.show"', b'op = "telegram.revoke"')
    with pytest.raises(ws.SpecError, match="isn't available"):
        ws.parse(files(bad))


def test_conditions():
    w = ws.check_when({"path": "steps.summary.outcome", "in": ["urgent"]}, "w")
    assert ws.condition(w, {"steps": {"summary": {"outcome": "urgent"}}})
    assert not ws.condition(w, {"steps": {"summary": {"outcome": "info"}}})


def test_trigger_identity_ignores_interval():
    a = ws._trigger({"type": "gmail.new", "query": "in:inbox", "every_minutes": 5}, 0)
    b = ws._trigger({"type": "gmail.new", "query": "in:inbox", "every_minutes": 15}, 0)
    c = ws._trigger({"type": "gmail.new", "query": "label:x"}, 0)
    assert ws.trigger_identity(a) == ws.trigger_identity(b) != ws.trigger_identity(c)
