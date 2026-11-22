"""One-time conversion of the old [[profiles.watchers]] into watchers v2: same triggers (batched, as before), one
agent step with the old instructions and tools, a notify step for its report. Trigger memory is carried over, so
nothing is reported twice. They are proposed to the owner like any new watcher (mode "ask each time" keeps today's
behaviour exactly)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from ampi_tools import watch_spec as ws

log = logging.getLogger(__name__)

MARKER = "_migrated_v2"
NO_REPLY = "NO_REPLY"


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _toml_list(xs) -> str:
    return "[" + ", ".join(_toml_str(x) for x in xs) + "]"


def convert(w: Any, tools: tuple[str, ...], instructions: str) -> tuple[dict[str, bytes], dict]:
    """(snapshot files, the new trigger as parsed) for one old watcher."""
    kind = w.kind
    if kind == "schedule":
        trig = f'type = "time.at"\nat = {_toml_list(w.at)}\ndays = {_toml_list(w.days)}'
        input_ = "Scheduled run for {{item.slot}} ({{item.timezone}}); it is now {{item.time}}."
    else:
        new_type, field = {"gmail": ("gmail.new", "query"), "todoist": ("todoist.new", "filter"),
                           "telegram": ("telegram.new", "chat"), "youtube": ("youtube.new", "channels"),
                           "threads": ("threads.reply", None), "instagram": ("instagram.comment", None)}[kind]
        every = max(float(w.every_minutes), ws.TRIGGERS[new_type]["every"][1])
        trig = f'type = "{new_type}"\n' + (f"{field} = {_toml_str(w.query)}\n" if field else "") + \
               f"every_minutes = {every:g}\nbatch = true"
        input_ = f'New items for the watcher "{w.name}":\n\n{{{{item.text}}}}'
    agent_tools = [t for t in tools if ws.AGENT_TOOL_RE.fullmatch(t)
                   and not (t.startswith("Bash(") and t[5:].split(":", 1)[0] in ws.AGENT_FORBIDDEN_BASH)]
    prompt = (instructions.strip() or "Tell the owner briefly what is new.") + (
        f"\n\nIf there is nothing worth telling the owner, reply with exactly {NO_REPLY} and nothing else. "
        "Otherwise your reply is sent to the owner as the watcher's report: short and worth reading.")
    toml = f'''name = {_toml_str(w.name)}
description = {_toml_str(f"Converted from the old {kind} watcher (same behaviour)")}

[[triggers]]
{trig}

[[steps]]
id = "agent"
type = "llm"
mode = "agent"
model = "sonnet"
prompt = "prompts/instructions.md"
input = {_toml_str(input_)}
tools = {_toml_list(agent_tools)}

[[steps]]
id = "report"
type = "notify"
text = "{{{{steps.agent.text}}}}"
when = {{ path = "steps.agent.text", not_in = ["{NO_REPLY}", ""] }}
'''
    files = {"watcher.toml": toml.encode(), "prompts/instructions.md": prompt.encode()}
    spec = ws.parse(files, expected_name=w.name)
    return files, spec.triggers[0]


async def migrate(engine: Any) -> list[str]:
    """Converts and proposes the profile's old watchers once. Returns the names proposed."""
    profile = engine.profile
    if not profile.watchers or engine.db.trigger_state(profile.name, MARKER, MARKER):
        return []
    from ..bot import watcher_tools

    done: list[str] = []
    for w in profile.watchers:
        if engine.db.trigger_state(profile.name, MARKER, w.name):  # this one was handled on an earlier start
            continue
        try:
            old_path = profile.workspace / "watchers" / f"{w.name}.md"
            instructions = old_path.read_text(encoding="utf-8") if old_path.is_file() else ""
            files, trigger = convert(w, watcher_tools(w, profile), instructions)
            folder = profile.workspace / "watchers" / w.name
            if not (folder / "watcher.toml").exists():  # never overwrite edits made since
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "prompts").mkdir(exist_ok=True)
                for p, d in files.items():
                    (folder / p).write_bytes(d)
            ident = ws.trigger_identity(trigger)
            old_state = engine.bot.store.get_watch_state(profile.name, w.name)
            if old_state is not None and engine.db.trigger_state(profile.name, w.name, ident) is None:
                old_state = {k: v for k, v in old_state.items() if k not in ("source", "runs")}
                engine.db.set_trigger_state(profile.name, w.name, ident, old_state)
            resp = await engine.tools({
                "cmd": "watch.propose", "name": w.name,
                "files": {p: base64.b64encode(d).decode() for p, d in files.items()},
                "request": f"(converted from your old watcher \"{w.name}\": {w.kind}"
                           + (f" {w.query}" if w.query else "") + ")",
                "requester": "the upgrade to watchers v2",
                "test_text": "Converted automatically from the old watcher: same trigger, same instructions and "
                             "tools. Choose \"ask each time\" to keep today's behaviour exactly."}, timeout=120)
            error = str(resp.get("error") or "")
            if resp.get("ok"):
                done.append(w.name)
            elif "already waiting" not in error and "already approved" not in error:
                log.warning("[%s] couldn't propose converted watcher %s: %s", profile.name, w.name, error)
                continue  # try again on the next start
            engine.db.set_trigger_state(profile.name, MARKER, w.name, {"proposed": bool(resp.get("ok"))})
        except Exception:
            log.exception("[%s] converting watcher %s failed", profile.name, w.name)
    if all(engine.db.trigger_state(profile.name, MARKER, w.name) for w in profile.watchers):
        engine.db.set_trigger_state(profile.name, MARKER, MARKER, {"done": [w.name for w in profile.watchers]})
    return done
