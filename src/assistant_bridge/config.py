"""Loading and validating config.toml."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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

    def group(self, chat_id: int) -> Group | None:
        return next((g for g in self.groups if g.chat_id == chat_id), None)


@dataclass(frozen=True)
class Config:
    claude_command: tuple[str, ...]
    db_path: Path
    max_concurrent_runs: int
    run_timeout_seconds: int
    profiles: tuple[Profile, ...]


def _str_list(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{field} must be a list of strings")
    return tuple(value)


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
        if any(x.chat_id == chat_id or x.name == name for x in groups):
            raise ConfigError(f"{gw}: duplicate group chat_id or name")
        groups.append(Group(chat_id=chat_id, name=name, batch_seconds=float(batch), fresh_session=fresh))
    return tuple(groups)


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
    if "--bare" in claude_command:
        raise ConfigError("claude_command must not contain --bare (it bypasses the subscription login)")

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
        if "--bare" in extra_args:
            raise ConfigError(f"{where}: extra_args must not contain --bare")

        groups = _parse_groups(p.get("groups", []), where)

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
            )
        )

    return Config(
        claude_command=claude_command,
        db_path=resolve(raw.get("db_path", "data/bridge.sqlite3")),
        max_concurrent_runs=_positive_int(raw.get("max_concurrent_runs", 2), "max_concurrent_runs"),
        run_timeout_seconds=_positive_int(raw.get("run_timeout_seconds", 1800), "run_timeout_seconds"),
        profiles=tuple(profiles),
    )
