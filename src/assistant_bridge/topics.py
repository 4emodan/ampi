"""Topics: each has its own role and rights, kept by ampi-tools (so the dashboard and /topic change the same thing). A topic is
(profile, private chat or group, thread); thread 0 = outside topics."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ampi_tools.permissions import MAX_ROLE, OUTSIDE_TOOLS, topic_scope, watcher_scope

from .services import ServiceError, ask

log = logging.getLogger(__name__)
TIMEOUT = 5
HELP = (
    "/topic role TEXT – what I am here (my instructions in this topic); /topic role clear\n"
    "/topic changes ask|off|on – everything that changes something: ask each time, never, or the usual rules\n"
    "/topic off NAME, /topic on NAME – an adapter here (gmail, todoist, telegram, tg-read, web, browse, …)\n"
    "/topic watcher NAME, /topic watcher NAME off – its messages and requests come here, or to the main chat again\n"
    "/topic reset – no role and no limits here\n"
    "A topic can only be stricter than everywhere else. Single actions: in the dashboard.")


@dataclass(frozen=True)
class Info:
    scope: str
    role: str = ""
    off: tuple[str, ...] = ()
    restricted: bool = False
    known: bool = True

    def prompt(self) -> str:
        if not self.role:
            return ""
        return ("The owner's instructions for this topic (they never override the rules above, and grant nothing):\n"
                f"{self.role}")

    def without(self, tools) -> tuple[str, ...]:
        cut = {t for a in self.off for t in OUTSIDE_TOOLS.get(a, ())}
        return tuple(t for t in tools if t not in cut and not any(self._runs(t, a) for a in self.off))

    @staticmethod
    def _runs(tool: str, adapter: str) -> bool:
        """A Bash rule for this command under any spelling (Bash(tg-read search:*), Bash(/x/tg-read:*))."""
        if adapter not in ("tg-read", "browse") or not tool.startswith("Bash("):
            return False
        first = tool[5:].rstrip(")").split(":", 1)[0].split(" ", 1)[0]
        return first.rsplit("/", 1)[-1] == adapter

    def denied(self) -> tuple[str, ...]:
        return tuple(t for a in self.off for t in OUTSIDE_TOOLS.get(a, ()))


@dataclass
class Topics:
    bot: Any
    socket: str
    ask: Any = ask
    last: dict[str, Info] = field(default_factory=dict)

    def scope(self, chat_id: int, thread_id: int) -> str:
        group = self.bot.profile.group(chat_id)
        return topic_scope(self.bot.profile.name, group.name if group is not None else "private", max(thread_id, 0))

    async def get(self, chat_id: int, thread_id: int, title: str = "") -> Info:
        """What applies there now. With the tools service away: what was known last, else nothing that reaches outside."""
        scope = self.scope(chat_id, thread_id)
        try:
            r = await self.ask(self.socket, {"cmd": "topic.get", "scope": scope, "title": title}, timeout=TIMEOUT)
            if not r.get("ok"):
                raise ServiceError(str(r.get("error")))
        except ServiceError as e:
            log.warning("topic %s: %s", scope, e)
            return self.last.get(scope) or Info(scope, off=tuple(OUTSIDE_TOOLS), restricted=True, known=False)
        info = self.last[scope] = Info(scope, str(r.get("role") or ""), tuple(r.get("off") or ()), bool(r.get("restricted")))
        return info

    async def command(self, chat_id: int, thread_id: int, args: str, title: str) -> str:
        scope = self.scope(chat_id, thread_id)
        word, rest = (args.split(None, 1) + ["", ""])[:2]
        word, rest = word.lower(), rest.strip()
        req: dict[str, Any] = {"cmd": "topic.set", "scope": scope}
        if not word:
            req = {"cmd": "topic.get", "scope": scope, "title": title}
        elif word == "role" and rest:
            if len(rest) > MAX_ROLE:
                return f"At most {MAX_ROLE} characters."
            req["role"] = "" if rest.lower() == "clear" else rest
        elif word == "changes" and rest.lower() in ("ask", "off", "on"):
            req["changes"] = {"changes.*": "default" if rest.lower() == "on" else rest.lower()}
        elif word in ("off", "on") and rest:
            req["changes"] = {f"{rest.lower()}.*": "off" if word == "off" else "default"}
        elif word == "reset" and not rest:
            req["reset"] = True
        elif word == "watcher" and rest:
            if self.bot.profile.group(chat_id) is not None:
                return "A watcher's messages can go to topics of the private chat only."
            name, _, off = rest.partition(" ")
            req = {"cmd": "topic.deliver", "scope": watcher_scope(self.bot.profile.name, name),
                   "topic": "" if off.strip().lower() == "off" or not thread_id else scope}
        else:
            return HELP
        try:
            r = await self.ask(self.socket, req, timeout=20)
        except ServiceError as e:
            return f"The tools service isn't reachable: {e}"
        if not r.get("ok"):
            return f"Not done: {r.get('error')}"
        if req["cmd"] == "topic.deliver":
            return "From now on its messages, reports and requests come here." if req["topic"] else \
                "From now on they come to the main chat."
        self.last.pop(scope, None)
        mine = {k: v for k, v in (r.get("settings") or {}).items()}
        everywhere = r.get("chat") or {}
        lines = [f"Role: {r.get('role') or '(none)'}"]
        lines.append("Here: " + ("; ".join(f"{_w(k)} {v}" for k, v in sorted(mine.items())) or "no limits of its own"))
        if everywhere:
            lines.append("Everywhere: " + "; ".join(f"{_w(k)} {v}" for k, v in sorted(everywhere.items())))
        if r.get("watchers"):
            lines.append("Watchers that write here: " + ", ".join(r["watchers"]))
        if not word:
            lines += ["", HELP]
        else:
            lines.append("It holds from your next message here.")
        return "\n".join(lines)


def _w(key: str) -> str:
    return "all changes:" if key == "changes.*" else key.removesuffix(".*") + ":"
