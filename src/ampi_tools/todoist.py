"""Todoist API v1 (https://developer.todoist.com/api/v1/) over httpx."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

BASE = "https://api.todoist.com/api/v1"
PAGE = 200
SAFE_PATH = re.compile(r"(/[A-Za-z0-9_-]{1,128})+")


class ToolError(Exception):
    """A problem the caller should see as is."""


def priority_label(api_priority: int | None) -> str | None:
    # The API counts the other way round: 4 is what the apps show as p1 (urgent).
    if not api_priority or api_priority == 1:
        return None
    return f"p{5 - api_priority}"


def api_priority(p: str | int | None) -> int | None:
    if p is None:
        return None
    s = str(p).lower().lstrip("p")
    if s not in ("1", "2", "3", "4"):
        raise ToolError(f"priority must be p1..p4, got {p!r}")
    return 5 - int(s)


class Todoist:
    def __init__(self, token: str, client: httpx.AsyncClient, base: str = BASE):
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client
        self._base = base
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        if not SAFE_PATH.fullmatch(path):  # ids are validated in ops.py too; this is the last line of defence
            raise ToolError(f"refusing odd API path {path!r}")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = await self._client.request(
                method, f"{self._base}{path}", params=params, json=body, headers=self._headers, timeout=30
            )
        except httpx.TransportError as e:
            raise ToolError(f"Todoist unreachable: {e!r}") from e
        if resp.status_code == 401 or resp.status_code == 403:
            raise ToolError("Todoist rejected the token (check TODOIST_TOKEN)")
        if resp.status_code == 404:
            raise ToolError(f"Todoist: not found ({path})")
        if resp.status_code >= 400:
            raise ToolError(f"Todoist error {resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise ToolError(f"Todoist sent an unexpected reply (HTTP {resp.status_code})") from None

    async def _all(self, path: str, params: dict | None = None, limit: int = 1000) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while True:
            data = await self._req("GET", path, {**(params or {}), "cursor": cursor, "limit": PAGE})
            items = data.get("results", data.get("items", [])) if isinstance(data, dict) else data
            out += items
            cursor = data.get("next_cursor") if isinstance(data, dict) else None
            if not cursor or len(out) >= limit:
                return out[:limit]

    async def _cached(self, key: str, loader) -> Any:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        value = await loader()
        self._cache[key] = (time.monotonic(), value)
        return value

    def forget(self) -> None:
        self._cache.clear()

    # --- reading ----------------------------------------------------------------------

    async def projects(self) -> list[dict]:
        return await self._cached("projects", lambda: self._all("/projects"))

    async def sections(self) -> list[dict]:
        return await self._cached("sections", lambda: self._all("/sections"))

    async def labels(self) -> list[dict]:
        return await self._cached("labels", lambda: self._all("/labels"))

    async def project_id(self, name_or_id: str) -> str:
        projects = await self.projects()
        for p in projects:
            if p["id"] == name_or_id:
                return p["id"]
        matches = [p for p in projects if p["name"].lower() == name_or_id.lower()]
        if not matches:
            names = ", ".join(p["name"] for p in projects)
            raise ToolError(f"no Todoist project {name_or_id!r} (projects: {names})")
        return matches[0]["id"]

    async def section_id(self, project_id: str, name_or_id: str) -> str:
        sections = [s for s in await self.sections() if s["project_id"] == project_id]
        for s in sections:
            if s["id"] == name_or_id or s["name"].lower() == name_or_id.lower():
                return s["id"]
        names = ", ".join(s["name"] for s in sections) or "none"
        raise ToolError(f"no section {name_or_id!r} in that project (sections: {names})")

    async def tasks(self, query: str | None = None, project: str | None = None, limit: int = 200) -> list[dict]:
        if query:
            return await self._all("/tasks/filter", {"query": query}, limit)
        pid = await self.project_id(project) if project else None
        return await self._all("/tasks", {"project_id": pid}, limit)

    async def task(self, task_id: str) -> dict:
        return await self._req("GET", f"/tasks/{task_id}")

    async def comments(self, task_id: str) -> list[dict]:
        return await self._all("/comments", {"task_id": task_id}, 100)

    async def completed(self, since: str, until: str, limit: int = 500) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            data = await self._req("GET", "/tasks/completed/by_completion_date",
                                   {"since": since, "until": until, "limit": PAGE, "cursor": cursor}) or {}
            out += data.get("items", [])
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return out[:limit]

    # --- writing ----------------------------------------------------------------------

    async def add_task(self, **fields: Any) -> dict:
        self.forget()
        return await self._req("POST", "/tasks", body={k: v for k, v in fields.items() if v is not None})

    async def update_task(self, task_id: str, **fields: Any) -> dict:
        return await self._req("POST", f"/tasks/{task_id}", body={k: v for k, v in fields.items() if v is not None})

    async def move_task(self, task_id: str, **target: Any) -> Any:
        return await self._req("POST", f"/tasks/{task_id}/move", body={k: v for k, v in target.items() if v})

    async def close_task(self, task_id: str) -> None:
        await self._req("POST", f"/tasks/{task_id}/close")

    async def reopen_task(self, task_id: str) -> None:
        await self._req("POST", f"/tasks/{task_id}/reopen")

    async def delete_task(self, task_id: str) -> None:
        await self._req("DELETE", f"/tasks/{task_id}")

    async def add_comment(self, task_id: str, content: str) -> dict:
        return await self._req("POST", "/comments", body={"task_id": task_id, "content": content})

    async def add_project(self, name: str, parent: str | None = None) -> dict:
        self.forget()
        parent_id = await self.project_id(parent) if parent else None
        return await self._req("POST", "/projects", body={"name": name, "parent_id": parent_id})

    async def add_section(self, project: str, name: str) -> dict:
        self.forget()
        return await self._req("POST", "/sections", body={"project_id": await self.project_id(project), "name": name})

    async def add_label(self, name: str) -> dict:
        self.forget()
        return await self._req("POST", "/labels", body={"name": name})

    # --- formatting -------------------------------------------------------------------

    async def describe(self, tasks: list[dict]) -> list[str]:
        projects = {p["id"]: p["name"] for p in await self.projects()}
        sections = {s["id"]: s["name"] for s in await self.sections()}
        return [format_task(t, projects, sections) for t in tasks]


def format_task(t: dict, projects: dict[str, str], sections: dict[str, str]) -> str:
    where = projects.get(t.get("project_id"), "?")
    if t.get("section_id"):
        where += f" / {sections.get(t['section_id'], '?')}"
    parts = [f"[{t['id']}] {t.get('content', '')}", where]
    due = t.get("due") or {}
    if due:
        when = due.get("string") or due.get("date")
        parts.append(f"due {when}" + (" (repeats)" if due.get("is_recurring") else ""))
    deadline = (t.get("deadline") or {}).get("date")
    if deadline:
        parts.append(f"deadline {deadline}")
    p = priority_label(t.get("priority"))
    if p:
        parts.append(p)
    if t.get("labels"):
        parts.append(" ".join(f"@{label}" for label in t["labels"]))
    if t.get("parent_id"):
        parts.append(f"subtask of {t['parent_id']}")
    line = "- " + " · ".join(parts)
    desc = (t.get("description") or "").strip()
    if desc:
        line += "\n  " + desc.replace("\n", "\n  ")[:500]
    return line
