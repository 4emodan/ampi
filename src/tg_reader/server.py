"""The `tg-reader` daemon: holds the owner's Telegram login and answers read-only requests.

    tg-reader login    once, interactively, as the tgreader user (you type the phone code yourself)
    tg-reader login-qr same, but you scan a QR code with your phone instead of typing a code
    tg-reader serve    run by systemd (deploy/tg-reader.service)

Settings come from the environment (deploy/tg-reader.env.example):
    TG_API_ID, TG_API_HASH   from https://my.telegram.org -> API development tools
    TG_SESSION               session file (default ~/tg-reader.session)
    TG_READER_SOCKET         Unix socket (default /run/tg-reader/tg-reader.sock)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from .reader import Reader

DEFAULT_SOCKET = "/run/tg-reader/tg-reader.sock"
MAX_REQUEST = 64 * 1024
log = logging.getLogger("tg-reader")


def socket_path() -> str:
    return os.environ.get("TG_READER_SOCKET") or DEFAULT_SOCKET


def _make_client():
    from telethon import TelegramClient  # only the daemon needs Telethon

    try:
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"].strip()
    except (KeyError, ValueError):
        sys.exit("Set TG_API_ID and TG_API_HASH (from https://my.telegram.org -> API development tools).")
    session = os.environ.get("TG_SESSION") or str(Path.home() / "tg-reader.session")
    return TelegramClient(
        session,
        api_id,
        api_hash,
        device_model="Ampi reader (read-only)",
        system_version="Linux",
        app_version="1.0",
        receive_updates=False,
    )


async def handle_connection(reader_obj: Reader, rd: asyncio.StreamReader, wr: asyncio.StreamWriter) -> None:
    try:
        try:
            line = await asyncio.wait_for(rd.readline(), 10)
            request = json.loads(line)  # ValueError if too long or not JSON
            if not isinstance(request, dict):
                raise ValueError
        except (ValueError, asyncio.TimeoutError):
            response = {"ok": False, "error": "bad request"}
        else:
            log.info("request: %s", request.get("cmd"))
            response = await reader_obj.handle(request)
        wr.write(json.dumps(response).encode() + b"\n")  # ASCII escapes: lone surrogates can't break it
        await wr.drain()
    except Exception:
        log.exception("connection failed")
    finally:
        wr.close()


async def serve_socket(reader_obj: Reader, path: str) -> asyncio.AbstractServer:
    p = Path(path)
    if p.exists() or p.is_symlink():
        p.unlink()
    server = await asyncio.start_unix_server(
        lambda r, w: handle_connection(reader_obj, r, w), path=path, limit=MAX_REQUEST + 1
    )
    os.chmod(path, 0o660)  # the tgread group (the assistant) may connect, nobody else
    return server


async def _serve() -> None:
    client = _make_client()
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Not logged in. Run `tg-reader login` as the tgreader user first.")
    me = await client.get_me()
    log.info("logged in as %s (id %s)", getattr(me, "first_name", "?"), me.id)
    deny = frozenset(int(x) for x in os.environ.get("TG_READER_DENY", "").replace(" ", "").split(",") if x)
    server = await serve_socket(Reader(client, deny=deny), socket_path())
    log.info("listening on %s", socket_path())
    async with server:
        serving = asyncio.ensure_future(server.serve_forever())
        disconnected = asyncio.ensure_future(client.disconnected)
        await asyncio.wait([serving, disconnected], return_when=asyncio.FIRST_COMPLETED)
    # Telethon gave up reconnecting (or the server stopped): exit so systemd restarts us cleanly.
    log.error("disconnected from Telegram; exiting")
    sys.exit(1)


async def _login() -> None:
    os.umask(0o077)  # the session file is as good as your Telegram password
    client = _make_client()
    print("Logging in to Telegram. Telegram will send a code to your Telegram app.")
    await client.start()  # asks for phone, code and 2FA password on this terminal
    me = await client.get_me()
    print(f"Logged in as {getattr(me, 'first_name', '')} (id {me.id}). Session saved; start the service:")
    print("  systemctl restart tg-reader")
    await client.disconnect()


QR_ROUNDS = 6  # each QR code lives about 30 s


async def _login_qr() -> None:
    import getpass

    import qrcode
    from telethon.errors import SessionPasswordNeededError

    os.umask(0o077)  # the session file is as good as your Telegram password
    client = _make_client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Already logged in. To log in again, delete the session file first.")
            return
        print("On your phone: Telegram -> Settings -> Devices -> Link Desktop Device, then scan this code.")
        qr = await client.qr_login()
        for _ in range(QR_ROUNDS):
            code = qrcode.QRCode(border=1)
            code.add_data(qr.url)
            code.print_ascii(invert=True)
            try:
                await qr.wait(timeout=30)
                break
            except asyncio.TimeoutError:
                print("That code expired; here's a fresh one.")
                await qr.recreate()
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Your Telegram cloud password (2FA): "))
                break
        else:
            sys.exit("No scan in time. Run `tg-reader login-qr` again.")
        me = await client.get_me()
        print(f"Logged in as {getattr(me, 'first_name', '')} (id {me.id}). Session saved; start the service:")
        print("  systemctl restart tg-reader")
    finally:
        await client.disconnect()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-reader", description="Read-only Telegram reader daemon")
    parser.add_argument("command", choices=["serve", "login", "login-qr"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run = {"serve": _serve, "login": _login, "login-qr": _login_qr}[args.command]
    asyncio.run(run())


if __name__ == "__main__":
    main()
