"""Talking to the helper daemons (ampi-tools, tg-reader) over their Unix sockets."""

from __future__ import annotations

import asyncio
import json


class ServiceError(Exception):
    pass


class ServiceUnavailable(ServiceError):
    """The request certainly wasn't delivered (the connection itself failed)."""


async def ask(path: str, payload: dict, *, timeout: float = 300) -> dict:
    """Sends one JSON request and returns the reply. Raises ServiceError if the service is unreachable."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=64 * 1024 * 1024), 10)
    except (OSError, asyncio.TimeoutError, AttributeError, NotImplementedError) as e:
        raise ServiceUnavailable(f"service at {path} is not reachable ({type(e).__name__})") from e
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout)
    except (OSError, asyncio.TimeoutError) as e:
        raise ServiceError(f"service at {path} failed: {type(e).__name__}") from e
    finally:
        writer.close()
    try:
        reply = json.loads(data)
    except ValueError as e:
        raise ServiceError(f"service at {path} sent a broken reply") from e
    if not isinstance(reply, dict):
        raise ServiceError(f"service at {path} sent a broken reply")
    return reply
