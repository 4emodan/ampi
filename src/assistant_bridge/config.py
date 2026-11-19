"""Loading and validating config.toml."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_ALLOWED_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "WebSearch", "WebFetch")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ConfigError(Exception):
    """The config file is missing something or has an invalid value."""


@dataclass(frozen=True)
class Group:
    """A Telegram group the bot acts in. Every message the owner posts or forwards there is processed."""

    chat_id: int
    name: str  # instructions live in <workspace>/groups/<name>.md
    batch_seconds: float  # messages arriving within this window are handled in one run
    fresh_session: bool  # new Claude session per batch (memory lives in files), or one ongoing session

    def instructions_path(self, workspace: Path) -> Path:
        return workspace / "groups" / f"{self.name}.md"


def zone(name: str) -> tzinfo:
    return timezone.utc if name == "UTC" else ZoneInfo(name)


WATCHER_KINDS =("gmail", "todoist", "telegram", "schedule")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Watcher:
    """Something the bridge checks on its own and lets Claude act on (see watchers.py)."""

    name: str  # instructions live in <workspace>/watchers/<name>.md
    kind: str  # gmail | todoist | telegram | schedule
    query: str  # gmail search, Todoist filter, or Telegram chat (name, @username or id)
    every_minutes: float  # how often to check (not for schedule)
    at: tuple[str, ...] = ()  # schedule: local times "HH:MM"
    days: tuple[str, ...] = DAYS  # schedule: weekdays
    # Tools for this watcher's runs (None = the profile's). Watchers read untrusted email and messages,
    # so a narrower set (e.g. without WebFetch) limits what injected text could make Claude do.
    allowed_tools: tuple[str, ...] | None = None

    def instructions_path(self, workspace: Path) -> Path:
        return workspace / "watchers" / f"{self.name}.md"


@dataclass(frozen=True)
class Tools:
    """Sockets of the optional helper services (Todoist/Gmail with approvals, Telegram reader)."""

    socket: str = "/run/ampi-tools/tools.sock"
    tg_reader_socket: str = "/run/tg-reader/tg-reader.sock"
    approvals: bool = False  # relay approval requests from ampi-tools to the owner


@dataclass(frozen=True)
class Profile:
    name: str
    bot_token_env: str
    bot_token: str
    owner_ids: frozenset[int]
    workspace: Path
    model: str | None
    allowed_tools: tuple[str, ...]
    extra_args: tuple[str, ...]
    groups: tuple[Group, ...] = ()
    watchers: tuple[Watcher, ...] = ()
    timezone: str = "UTC"

    @property
    def owner_chat_id(self) -> int:
        """The owner's private chat with the bot (same number as their user ID)."""
        return min(self.owner_ids)

    def group(self, chat_id: int) -> Group | None:
        return next((g for g in self.groups if g.chat_id == chat_id), None)


@dataclass(frozen=True)
class Config:
    claude_command: tuple[str, ...]
    db_path: Path
    max_concurrent_runs: int
    run_timeout_seconds: int
    profiles: tuple[Profile, ...]
    tools: Tools = Tools()
    # Root-owned Claude Code settings passed to every run (deploy/claude-settings.json: blocks reads and
    # writes outside the workspace). None = not passed.
    claude_settings: Path | None = None


def _str_list(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{field} must be a list of strings")
    return tuple(value)


# Flags that would bypass the subscription login or the permission rules the bridge sets.
FORBIDDEN_FLAGS = (
    "--bare", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--permission-mode",
    "--add-dir", "--settings", "--setting-sources", "--mcp-config", "--allowedTools", "--allowed-tools",
    "--disallowedTools", "--disallowed-tools", "--plugin-dir",
)


def _check_flags(args: tuple[str, ...], field: str) -> None:
    for a in args:
        if a.split("=", 1)[0] in FORBIDDEN_FLAGS:
            raise ConfigError(f"{field} must not contain {a.split('=', 1)[0]} (the bridge controls permissions)")


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{field} must be a positive integer")
    return value


def _parse_groups(raw: object, where: str) -> tuple[Group, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: groups must be a list of [[profiles.groups]] tables")
    groups: list[Group] = []
    for j, g in enumerate(raw):
        gw = f"{where} groups[{j}]"
        if not isinstance(g, dict):
            raise ConfigError(f"{gw} must be a table")
        chat_id = g.get("chat_id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id >= 0:
            raise ConfigError(f"{gw}: chat_id must be the group's (negative) Telegram chat ID")
        name = g.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ConfigError(f"{gw}: name must be lowercase letters, digits, '-' or '_'")
        batch = g.get("batch_seconds", 5)
        if not isinstance(batch, (int, float)) or isinstance(batch, bool) or not 0 <= batch <= 300:
            raise ConfigError(f"{gw}: batch_seconds must be between 0 and 300")
        fresh = g.get("fresh_session", True)
        if not isinstance(fresh, bool):
            raise ConfigError(f"{gw}: fresh_session must be true or false")
        if name.startswith("watch-") or name == "private":
            raise ConfigError(f"{gw}: group names can't be 'private' or start with 'watch-' (used for logs)")
        if any(x.chat_id == chat_id or x.name == name for x in groups):
            raise ConfigError(f"{gw}: duplicate group chat_id or name")
        groups.append(Group(chat_id=chat_id, name=name, batch_seconds=float(batch), fresh_session=fresh))
    return tuple(groups)


def _parse_watchers(raw: object, where: str) -> tuple[Watcher, ...]:
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: watchers must be a list of [[profiles.watchers]] tables")
    out: list[Watcher] = []
    for j, w in enumerate(raw):
        ww = f"{where} watchers[{j}]"
        if not isinstance(w, dict):
            raise ConfigError(f"{ww} must be a table")
        name = w.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ConfigError(f"{ww}: name must be lowercase letters, digits, '-' or '_'")
        kind = w.get("kind")
        if kind not in WATCHER_KINDS:
            raise ConfigError(f"{ww}: kind must be one of {', '.join(WATCHER_KINDS)}")
        query = w.get("query", "")
        if not isinstance(query, str) or (kind != "schedule" and not query.strip()):
            raise ConfigError(f"{ww}: query is required (gmail search, Todoist filter or Telegram chat)")
        every = w.get("every_minutes", 10)
        if not isinstance(every, (int, float)) or isinstance(every, bool) or not 1 <= every <= 1440:
            raise ConfigError(f"{ww}: every_minutes must be between 1 and 1440")
        if kind != "schedule" and ("at" in w or "days" in w):
            raise ConfigError(f"{ww}: at/days only apply to kind = \"schedule\"")
        tools = w.get("allowed_tools")
        tools = None if tools is None else _str_list(tools, f"{ww}.allowed_tools")
        at = _str_list(w.get("at", []), f"{ww}.at")
        if kind == "schedule":
            if not at:
                raise ConfigError(f"{ww}: a schedule needs at = [\"HH:MM\", ...]")
            for t in at:
                if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                    raise ConfigError(f"{ww}: bad time {t!r} (use HH:MM, 24-hour)")
        days = tuple(d.lower()[:3] for d in _str_list(w.get("days", list(DAYS)), f"{ww}.days"))
        if not days or any(d not in DAYS for d in days):
            raise ConfigError(f"{ww}: days must be from {', '.join(DAYS)}")
        if any(x.name == name for x in out):
            raise ConfigError(f"{ww}: duplicate watcher name")
        out.append(Watcher(name=name, kind=kind, query=query.strip(), every_minutes=float(every), at=at, days=days,
                           allowed_tools=tools))
    return tuple(out)


def _settings_path(raw: object, resolve) -> Path | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ConfigError("claude_settings must be a path")
    path = resolve(raw)
    if not path.is_file():
        raise ConfigError(f"claude_settings: {path} not found")
    return path


def _parse_tools(raw: object) -> Tools:
    if raw is None:
        return Tools()
    if not isinstance(raw, dict):
        raise ConfigError("[tools] must be a table")
    approvals = raw.get("approvals", False)
    if not isinstance(approvals, bool):
        raise ConfigError("tools.approvals must be true or false")
    defaults = Tools()
    sock = raw.get("socket", defaults.socket)
    tg = raw.get("tg_reader_socket", defaults.tg_reader_socket)
    if not isinstance(sock, str) or not isinstance(tg, str):
        raise ConfigError("tools sockets must be paths")
    return Tools(socket=sock, tg_reader_socket=tg, approvals=approvals)


def load_config(path: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
    base = path.parent

    def resolve(p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (base / q).resolve()

    claude_command = _str_list(raw.get("claude_command", ["claude"]), "claude_command")
    if not claude_command:
        raise ConfigError("claude_command must not be empty")
    _check_flags(claude_command, "claude_command")

    profiles_raw = raw.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise ConfigError("config needs at least one [[profiles]] block")

    profiles: list[Profile] = []
    seen_names: set[str] = set()
    seen_tokens: set[str] = set()
    for i, p in enumerate(profiles_raw):
        where = f"profiles[{i}]"
        if not isinstance(p, dict):
            raise ConfigError(f"{where} must be a table")
        name = p.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ConfigError(f"{where}.name must be lowercase letters, digits, '-' or '_'")
        where = f"profile '{name}'"
        if name in seen_names:
            raise ConfigError(f"duplicate profile name '{name}'")
        seen_names.add(name)

        token_env = p.get("bot_token_env")
        if not isinstance(token_env, str) or not token_env:
            raise ConfigError(f"{where}: bot_token_env is required")
        token = env.get(token_env, "").strip()
        if not token:
            raise ConfigError(f"{where}: environment variable {token_env} is not set")
        if token in seen_tokens:
            raise ConfigError(f"{where}: two profiles use the same bot token")
        seen_tokens.add(token)

        owners = p.get("owner_ids")
        if (
            not isinstance(owners, list)
            or not owners
            or not all(isinstance(o, int) and not isinstance(o, bool) for o in owners)
        ):
            raise ConfigError(f"{where}: owner_ids must be a non-empty list of Telegram user IDs")

        model = p.get("model")
        if model is not None and (not isinstance(model, str) or not model):
            raise ConfigError(f"{where}: model must be a string")

        extra_args = _str_list(p.get("extra_args", []), f"{where}.extra_args")
        _check_flags(extra_args, f"{where}: extra_args")

        groups = _parse_groups(p.get("groups", []), where)
        watchers = _parse_watchers(p.get("watchers", []), where)
        tz = p.get("timezone", "UTC")
        try:
            zone(tz)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ConfigError(f"{where}: unknown timezone {tz!r} (e.g. \"Europe/Athens\")") from None

        profiles.append(
            Profile(
                name=name,
                bot_token_env=token_env,
                bot_token=token,
                owner_ids=frozenset(owners),
                workspace=resolve(p.get("workspace", f"profiles/{name}")),
                model=model,
                allowed_tools=_str_list(
                    p.get("allowed_tools", list(DEFAULT_ALLOWED_TOOLS)), f"{where}.allowed_tools"
                ),
                extra_args=extra_args,
                groups=groups,
                watchers=watchers,
                timezone=tz,
            )
        )

    tools = _parse_tools(raw.get("tools"))
    if tools.approvals and len({o for p in profiles for o in p.owner_ids}) != 1:
        raise ConfigError("[tools] approvals = true needs exactly one owner across all profiles")

    return Config(
        claude_command=claude_command,
        db_path=resolve(raw.get("db_path", "data/bridge.sqlite3")),
        max_concurrent_runs=_positive_int(raw.get("max_concurrent_runs", 2), "max_concurrent_runs"),
        run_timeout_seconds=_positive_int(raw.get("run_timeout_seconds", 1800), "run_timeout_seconds"),
        profiles=tuple(profiles),
        tools=tools,
        claude_settings=_settings_path(raw.get("claude_settings"), resolve),
    )
