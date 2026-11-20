"""Runs one conversation turn through the official Claude Code CLI (`claude -p`)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Environment variables that would switch Claude Code from the subscription login to
# pay-per-use API billing. They are never passed to the CLI.
BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_MISSING_SESSION = re.compile(r"no conversation found", re.I)
_SESSION_IN_USE = re.compile(r"already in use", re.I)


@dataclass(frozen=True)
class RunResult:
    ok: bool
    text: str
    error: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    timed_out: bool = False
    cancelled: bool = False  # stopped by the owner (/cancel)


def clean_env(environ: Mapping[str, str], secret_names: Iterable[str]) -> dict[str, str]:
    """Copy of the environment without bot tokens and API-billing credentials."""
    drop = set(BILLING_ENV_VARS) | set(secret_names)
    return {k: v for k, v in environ.items() if k not in drop}


# A bare "Edit"/"Write" rule would let Claude write anywhere the assistant user can (tested: in -p mode
# it does). File edits are limited to the working directory (the profile workspace) instead.
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
WORKSPACE_EDIT = "Edit(./**)"

# Reads Claude must never make, whatever the profile allows: secrets of the bridge, Claude's own login,
# other processes' memory and environment. (Grep and Glob follow Read rules.)
DENIED_TOOLS = (
    "Read(//proc/**)",
    "Read(//etc/claude-assistant/**)",
    "Read(~/.config/**)",
    "Read(~/.claude/**)",
    "Read(~/.claude.json)",
    "Read(~/config.toml)",
    "Read(~/config.toml.*)",
    "Read(~/.ssh/**)",
    "Read(~/data/**)",
    # Inside the workspace: files that would give Claude new permissions, hooks, MCP servers or a
    # rewritten persona on every later run.
    "Edit(./.claude/**)",
    "Edit(./.mcp.json)",
    "Edit(./CLAUDE.md)",
    "Edit(./CLAUDE.local.md)",
    "Edit(./**/CLAUDE.md)",
    "Edit(./**/CLAUDE.local.md)",
)


def scope_tools(allowed: Sequence[str]) -> list[str]:
    out: list[str] = []
    for tool in allowed:
        base = tool.split("(", 1)[0]
        # Any edit rule becomes "the workspace only"; a path rule like Edit(//**) is never passed on.
        if base in _EDIT_TOOLS and (not tool.startswith(("Edit(./", "Write(./")) or ".." in tool):
            tool = WORKSPACE_EDIT
        if tool not in out:
            out.append(tool)
    return out


def build_args(
    command: Sequence[str],
    *,
    session_id: str,
    resume: bool,
    model: str | None,
    allowed_tools: Sequence[str],
    append_system_prompt: str | None,
    extra_args: Sequence[str] = (),
    settings_file: str | None = None,
) -> list[str]:
    args = [*command, "-p", "--output-format", "json", "--strict-mcp-config"]
    if settings_file:  # root-owned; blocks all reads and writes outside the workspace (deploy/claude-settings.json)
        args += ["--settings", settings_file]
    args += ["--resume", session_id] if resume else ["--session-id", session_id]
    if model:
        args += ["--model", model]
    if allowed_tools:
        args += ["--allowedTools", ",".join(scope_tools(allowed_tools))]
    args += ["--disallowedTools", ",".join(DENIED_TOOLS)]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    args += list(extra_args)
    return args


def parse_output(returncode: int, stdout: str, stderr: str) -> RunResult:
    payload: dict | None = None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
    if payload is None:
        detail = (stderr.strip() or stdout.strip() or f"exit code {returncode}")[-1500:]
        return RunResult(ok=False, text="", error=detail)

    text = payload.get("result") or ""
    is_error = bool(payload.get("is_error")) or payload.get("subtype", "success") != "success"
    cost = payload.get("total_cost_usd")
    duration = payload.get("duration_ms")
    if is_error or returncode != 0:
        detail = text or payload.get("subtype") or stderr.strip()[-1500:] or f"exit code {returncode}"
        return RunResult(ok=False, text=text, error=str(detail), cost_usd=cost, duration_ms=duration)
    return RunResult(ok=True, text=text, cost_usd=cost, duration_ms=duration)


class ClaudeRunner:
    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        max_concurrent: int,
        env: Mapping[str, str] | None = None,
        settings_file: str | None = None,
    ):
        self._settings_file = settings_file
        self._command = tuple(command)
        self._timeout = timeout_seconds
        self._sem = asyncio.Semaphore(max_concurrent)
        self._env = dict(env) if env is not None else clean_env(os.environ, ())

    async def _exec(self, args: list[str], prompt: str, cwd: Path, origin: str | None = None) -> RunResult:
        env = self._env
        if origin:
            # Tells the tool commands (and so the owner's approval requests) that this run handles outside
            # content. Claude can't drop it: the allowed `Bash(tool:*)` rules don't match `env`/`unset` prefixes.
            env = {**self._env, "AMPI_ORIGIN": origin}
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), self._timeout)
        except asyncio.TimeoutError:
            await _stop(proc)
            return RunResult(ok=False, text="", error=f"stopped after {int(self._timeout)} s", timed_out=True)
        except asyncio.CancelledError:
            await _stop(proc)
            raise
        return parse_output(
            proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        )

    async def run_turn(
        self,
        prompt: str,
        *,
        cwd: Path,
        session_id: str,
        started: bool,
        model: str | None,
        allowed_tools: Sequence[str],
        append_system_prompt: str | None = None,
        extra_args: Sequence[str] = (),
        origin: str | None = None,
    ) -> RunResult:
        """Send one message to the conversation's Claude session. `origin` (e.g. "watcher:gmail-inbox") marks
        runs that handle outside content rather than the owner's own messages.

        A new conversation creates the session with --session-id; later turns use --resume.
        If the stored state disagrees with Claude Code's (session missing or already created),
        retries once the other way, keeping the same session ID.
        """
        async with self._sem:
            resume = started
            result = RunResult(ok=False, text="", error="not run")
            for attempt in range(2):
                args = build_args(
                    self._command,
                    session_id=session_id,
                    resume=resume,
                    model=model,
                    allowed_tools=allowed_tools,
                    append_system_prompt=append_system_prompt,
                    extra_args=extra_args,
                    settings_file=self._settings_file,
                )
                result = await self._exec(args, prompt, cwd, origin)
                if result.ok or attempt == 1 or not result.error:
                    break
                if resume and _MISSING_SESSION.search(result.error):
                    log.warning("session %s not found by Claude Code, starting it fresh", session_id)
                    resume = False
                elif not resume and _SESSION_IN_USE.search(result.error):
                    log.warning("session %s already exists, resuming it", session_id)
                    resume = True
                else:
                    break
            return result


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 10)
    except ProcessLookupError:
        return
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
