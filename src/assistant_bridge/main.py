"""Entry point: `claude-assistant --config config.toml`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys

import httpx

from .bot import ProfileBot
from .claude_runner import BILLING_ENV_VARS, ClaudeRunner, clean_env
from .config import Config, ConfigError, load_config
from .store import Store
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("assistant_bridge")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claude-assistant", description=__doc__)
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--check", action="store_true", help="validate config, bots and the claude CLI, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs, which contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        log.error("config error: %s", e)
        return 2

    for var in BILLING_ENV_VARS:
        if os.environ.get(var):
            log.warning("%s is set; it will NOT be passed to claude (subscription login is used)", var)

    if args.check:
        return asyncio.run(check(config))
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        pass
    return 0


async def check(config: Config) -> int:
    ok = True
    try:
        version = subprocess.run(
            [*config.claude_command, "--version"], capture_output=True, text=True, timeout=60
        )
        if version.returncode == 0:
            print(f"claude CLI: {version.stdout.strip()}")
        else:
            print(f"claude CLI: failed ({version.stderr.strip()})")
            ok = False
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"claude CLI: not runnable ({e})")
        ok = False

    async with httpx.AsyncClient() as client:
        for p in config.profiles:
            try:
                me = await TelegramAPI(p.bot_token, client).get_me()
                print(f"profile {p.name}: bot @{me.get('username')}, workspace {p.workspace}")
            except TelegramError as e:
                print(f"profile {p.name}: Telegram error: {e}")
                ok = False
            if not (p.workspace / "CLAUDE.md").is_file():
                print(f"profile {p.name}: note: no CLAUDE.md in workspace (persona/instructions)")
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


async def serve(config: Config) -> None:
    store = Store(config.db_path)
    env = clean_env(os.environ, [p.bot_token_env for p in config.profiles])
    runner = ClaudeRunner(
        config.claude_command,
        timeout_seconds=config.run_timeout_seconds,
        max_concurrent=config.max_concurrent_runs,
        env=env,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)

    async with httpx.AsyncClient() as client:
        bots = []
        for p in config.profiles:
            p.workspace.mkdir(parents=True, exist_ok=True)
            bots.append(ProfileBot(p, TelegramAPI(p.bot_token, client), store, runner))
        pollers = [asyncio.create_task(b.poll_forever(), name=f"poll-{b.profile.name}") for b in bots]
        stopper = asyncio.create_task(stop.wait())
        log.info("running %d profile(s): %s", len(bots), ", ".join(p.name for p in config.profiles))
        done, _ = await asyncio.wait([stopper, *pollers], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper and task.exception():
                log.error("poller %s crashed", task.get_name(), exc_info=task.exception())
        log.info("shutting down")
        for task in [stopper, *pollers]:
            task.cancel()
        await asyncio.gather(stopper, *pollers, return_exceptions=True)
        await asyncio.gather(*(b.shutdown() for b in bots), return_exceptions=True)
    store.close()


if __name__ == "__main__":
    sys.exit(main())
