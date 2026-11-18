"""Minimal async client for the Telegram Bot API.

Written directly against the HTTP API (not a wrapper library) so new Bot API
features such as topics in private chats work without waiting for a library update.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, method: str, description: str, error_code: int | None = None):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient, base_url: str = API_BASE):
        self._token = token
        self._client = client
        self._base = base_url.rstrip("/")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 30.0,
        attempts: int = 5,
    ) -> Any:
        url = f"{self._base}/bot{self._token}/{method}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(1, attempts + 1):
            try:
                if files:
                    data = {
                        k: v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                        for k, v in params.items()
                    }
                    resp = await self._client.post(url, data=data, files=files, timeout=timeout)
                else:
                    resp = await self._client.post(url, json=params, timeout=timeout)
                payload = resp.json()
            except (httpx.TransportError, ValueError) as e:
                if attempt == attempts:
                    raise TelegramError(method, f"network error: {e!r}") from e
                await asyncio.sleep(min(2**attempt, 30))
                continue

            if payload.get("ok"):
                return payload.get("result")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            code = payload.get("error_code")
            if retry_after and attempt < attempts:
                await asyncio.sleep(float(retry_after))
                continue
            if code is not None and code >= 500 and attempt < attempts:
                await asyncio.sleep(min(2**attempt, 30))
                continue
            raise TelegramError(method, payload.get("description", "unknown error"), code)
        raise TelegramError(method, "gave up after retries")

    # --- methods used by the bridge -------------------------------------------------

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 15,
            attempts=1,
        )

    async def set_my_commands(self, commands: list[dict]) -> None:
        await self.call("setMyCommands", {"commands": commands})

    async def send_message(
        self, chat_id: int, text: str, thread_id: int | None = None, parse_mode: str | None = None
    ) -> dict:
        return await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "message_thread_id": thread_id or None,
                "parse_mode": parse_mode,
                "link_preview_options": {"is_disabled": True},
            },
        )

    async def send_chat_action(self, chat_id: int, action: str, thread_id: int | None = None) -> None:
        await self.call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action, "message_thread_id": thread_id or None},
            attempts=1,
        )

    async def create_forum_topic(self, chat_id: int, name: str) -> dict:
        return await self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", {"file_id": file_id})

    async def download_file(self, file_path: str, dest: Path) -> None:
        url = f"{self._base}/file/bot{self._token}/{file_path}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            async with self._client.stream("GET", url, timeout=120) as resp:
                if resp.status_code != 200:
                    raise TelegramError("download", f"HTTP {resp.status_code}")
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
            tmp.replace(dest)
        except httpx.TransportError as e:
            raise TelegramError("download", f"network error: {e!r}") from e
        finally:
            tmp.unlink(missing_ok=True)

    async def send_file(
        self, chat_id: int, path: Path, thread_id: int | None = None, as_photo: bool = False
    ) -> dict:
        method, field = ("sendPhoto", "photo") if as_photo else ("sendDocument", "document")
        with path.open("rb") as f:
            return await self.call(
                method,
                {"chat_id": chat_id, "message_thread_id": thread_id or None},
                files={field: (path.name, f)},
                timeout=300,
                attempts=1,
            )
