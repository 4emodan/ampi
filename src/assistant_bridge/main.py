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
import tempfile

import httpx

from .approvals import ApprovalRelay
from .bot import ProfileBot
from .permissions import PermissionBroker
from .watch.control import Control
from .watch.db import WatchDB
from .watch.engine import WatchEngine
from .watch.migrate import migrate
from .watch.tgui import WatchUI
from .info_ui import InfoUI
from .claude_runner import BILLING_ENV_VARS, ClaudeRunner, clean_env
from .config import Config, ConfigError, load_config
from .services import ask
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


def protected_chats(config: Config, store: Store, wdb: WatchDB | None = None,
                    engines: dict | None = None) -> tuple[list[int], bool]:
    """Chats the assistant reads itself, where a message from the owner's account would count as the owner's
    own command: every profile's bot, its groups, and the chats its Telegram watchers follow. Returns
    (ids, complete); incomplete while a Telegram watcher hasn't learned its chat's id yet."""
    ids: list[int] = []
    complete = True
    for p in config.profiles:
        prefix = p.bot_token.split(":", 1)[0]
        if prefix.isdigit():
            ids.append(int(prefix))
        ids += [g.chat_id for g in p.groups]
        for w in p.watchers:
            if w.kind == "telegram":
                peer = (store.get_watch_state(p.name, w.name) or {}).get("peer_id")
                if isinstance(peer, int) and not isinstance(peer, bool):
                    ids.append(peer)
                else:
                    complete = False
    # Watchers v2: chats their telegram.new triggers read.
    for name, engine in (engines or {}).items():
        from ampi_tools.watch_spec import trigger_identity
        for w in engine.watchers.values():
            for t in w.spec.triggers:
                if t["type"] != "telegram.new":
                    continue
                peer = (wdb.trigger_state(name, w.name, trigger_identity(t)) or {}).get("peer_id") if wdb else None
                if isinstance(peer, int) and not isinstance(peer, bool):
                    ids.append(peer)
                else:
                    complete = False
    return sorted(set(ids)), complete


def make_undumpable() -> bool:
    """PR_SET_DUMPABLE=0: /proc/<pid>/environ, /mem etc. of this process become root-only, so the
    claude runs (same Unix user) can't read the bot token from the bridge's environment or memory,
    and can't impersonate the bridge to ampi-tools. Linux only; returns whether it worked."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 4
    except (OSError, AttributeError):
        return False


async def serve(config: Config) -> None:
    if config.claude_settings is None:
        log.warning("claude_settings is not set: Claude runs can read files outside their workspace "
                    "(set claude_settings = \"/home/assistant/app/deploy/claude-settings.json\")")
    if not make_undumpable():
        log.warning("could not make the process non-dumpable; the bot token is visible in /proc to the same user")
    store = Store(config.db_path)
    wdb = WatchDB(config.db_path)
    run_dir = os.environ.get("RUNTIME_DIRECTORY") or tempfile.gettempdir()  # systemd: /run/claude-assistant
    watchers_socket = os.path.join(run_dir, "watchers.sock")
    os.environ["AMPI_WATCHERS_SOCKET"] = watchers_socket  # the `watchers` command in claude runs finds the bridge
    env = clean_env(os.environ, [p.bot_token_env for p in config.profiles])
    runner = ClaudeRunner(
        config.claude_command,
        timeout_seconds=config.run_timeout_seconds,
        max_concurrent=config.max_concurrent_runs,
        env=env,
        settings_file=str(config.claude_settings) if config.claude_settings else None,
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
            dot = p.workspace / ".claude"
            if hasattr(os, "getuid") and (not dot.is_dir() or dot.stat().st_uid != 0):
                log.warning("[%s] %s is missing or not owned by root: Claude could give itself settings or hooks. "
                            "Run deploy/install.sh as root.", p.name, dot)
            bots.append(ProfileBot(p, TelegramAPI(p.bot_token, client), store, runner))
        own_bots = sorted({i for b in bots for i in b.own_bot_ids})
        engines: dict[str, WatchEngine] = {}
        for b in bots:
            b.own_bot_ids = set(own_bots)
            engine = WatchEngine(b, wdb, tools_socket=config.tools.socket, tg_socket=config.tools.tg_reader_socket,
                                 data_dir=config.db_path.parent)
            engines[b.profile.name] = engine
            b.watch_ui = WatchUI(b, engine)
            b.info_ui = InfoUI(b, config.tools.socket) if config.tools.approvals else None
        control = Control(engines, watchers_socket)
        await control.start()
        for b in bots:
            if b.profile.ask_permissions:
                b.permissions = PermissionBroker(b, os.path.join(run_dir, f"permission-{b.profile.name}.sock"),
                                                 config.tools.pkg_socket or None)
                await b.permissions.start()
        pollers = [asyncio.create_task(b.poll_forever(), name=f"poll-{b.profile.name}") for b in bots]
        if config.tools.approvals:
            # Approval requests go to the first profile's owner chat (one relay, so no duplicates).
            bots[0].approvals = ApprovalRelay(bots[0], config.tools.socket,
                                              protected=lambda: protected_chats(config, store, wdb, engines))
            bots[0].approvals.own_bots = own_bots
            pollers.append(asyncio.create_task(bots[0].approvals.run_forever(), name="approvals"))
            # ampi-tools learns which process each claude run is and where it comes from.
            socket_path = config.tools.socket

        else:
            socket_path = None

        async def on_spawn(pid: int, origin: str, tag: object = None) -> None:
            await control.on_spawn(pid, origin, tag)
            # The builder never talks to ampi-tools; leaving it unregistered makes it "unknown" there.
            if socket_path is None or (isinstance(tag, tuple) and tag and tag[0] == "builder"):
                return
            reply = await ask(socket_path, {"cmd": "runs.register", "pid": pid, "origin": origin}, timeout=10)
            if not reply.get("ok"):  # the run then counts as "unknown" (everything asks the owner)
                log.warning("ampi-tools refused to register run %s: %s", pid, reply.get("error"))

        async def on_exit(pid: int) -> None:
            await control.on_exit(pid)
            if socket_path is not None:
                await ask(socket_path, {"cmd": "runs.unregister", "pid": pid}, timeout=10)

        runner.on_spawn, runner.on_exit = on_spawn, on_exit
        background: list[asyncio.Task] = []
        for name, engine in engines.items():
            pollers.append(asyncio.create_task(engine.run_forever(), name=f"watchers-{name}"))

            async def migrate_later(e: WatchEngine = engine) -> None:
                await asyncio.sleep(20)
                try:
                    done = await migrate(e)
                    if done:
                        await e.notify("Watchers were upgraded. Your old watchers are converted and wait for your "
                                       f"approval (one request each): {', '.join(done)}. Until you approve them, they "
                                       "don't run. \"Ask each time\" keeps today's behaviour exactly.")
                except Exception:
                    log.exception("[%s] watcher migration failed", e.profile.name)
            if config.tools.approvals:  # a one-off task: not in pollers (any poller ending stops the bridge)
                background.append(asyncio.create_task(migrate_later(), name=f"migrate-{name}"))
        stopper = asyncio.create_task(stop.wait())
        log.info("running %d profile(s): %s", len(bots), ", ".join(p.name for p in config.profiles))
        done, _ = await asyncio.wait([stopper, *pollers], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not stopper and task.exception():
                log.error("poller %s crashed", task.get_name(), exc_info=task.exception())
        log.info("shutting down")
        for task in [stopper, *pollers, *background]:
            task.cancel()
        await asyncio.gather(stopper, *pollers, return_exceptions=True)
        await asyncio.gather(*(b.shutdown() for b in bots), return_exceptions=True)
        await asyncio.gather(*(e.shutdown() for e in engines.values()), return_exceptions=True)
        await control.close()
    store.close()


if __name__ == "__main__":
    sys.exit(main())
