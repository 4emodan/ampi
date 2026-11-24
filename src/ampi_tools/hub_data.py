"""What the Ampi hub's pages show and change. Runs in the ampi-tools daemon (the only writer); the hub asks over its socket,
checked by user id. Anything looser than now is only requested here: the owner approves it in Telegram."""

from __future__ import annotations

import json
from typing import Any

from . import ops_flow, permissions as perms, watch_spec as ws
from .cards import LABELS
from .ops import ALWAYS_CRITICAL, OPS
from .ops_checkout import PRIVATE_ONLY
from .todoist import ToolError

ADAPTER_NAMES = {"todoist": "Todoist", "gmail": "Gmail", "youtube": "YouTube", "threads": "Threads", "instagram": "Instagram",
                 "igweb": "Instagram (your session)", "thweb": "Threads (your session)", "telegram": "Telegram as you",
                 "maps": "Google Maps", "wolt": "Wolt", "info": "Personal info", "checkout": "Checkout browser",
                 "payment": "Payments", "phone": "Your phone", "signin": "Saved logins", "tg-read": "Reading your Telegram",
                 "web": "Web search"}


def critical_ops(d: Any) -> set[str]:
    return {n for n in OPS if d.policy.critical(n)}


def rule(d: Any, scope: str, op: str) -> str:
    """The built-in rule, in a few words."""
    if scope != "chat":
        if op in ws.NOT_FOR_WATCHERS:
            return "never for watchers"
        if not ws.is_write(op):
            return "reads freely"
        return "labels up to 25 emails freely" if op == "gmail.label" else "asks, unless allowed for this watcher"
    kind = op.split(".", 1)[0]
    if op in PRIVATE_ONLY or kind == "signin":
        return "your private chat only; " + ("asks" if d.policy.critical(op) else "part of a task you approve")
    if kind == "telegram" and ws.is_write(op):
        return "asks, unless you allowed the chat"
    if kind == "phone":
        return "your chat freely; groups ask"
    if op in ALWAYS_CRITICAL:
        return "always asks"
    if d.policy.critical(op):
        return "asks"
    if ws.is_write(op):
        return "does it" if kind == "gmail" else "does it; asks when others would see it"
    return "reads freely"


def check_changes(d: Any, scope: str, changes: Any) -> None:
    if not isinstance(scope, str) or not perms.SCOPE_RE.fullmatch(scope):
        raise ToolError("bad scope")
    if scope != "chat":
        profile, name = scope.removeprefix("watcher:").split("/", 1)
        w = d.keeper.watcher(profile, name) if d.keeper else None
        if not w or w["status"] in ("deleted", "proposed"):
            raise ToolError("no such watcher")
    if not isinstance(changes, dict) or not 1 <= len(changes) <= 60:
        raise ToolError("no changes")
    for op, pair in changes.items():
        if not (isinstance(pair, list) and len(pair) == 2 and all(m in perms.RANK for m in pair) and pair[0] != pair[1]):
            raise ToolError("bad change")
        if pair[1] != "default" and pair[1] not in perms.allowed_modes(scope, op, OPS, critical_ops(d)):
            raise ToolError(f"{op} can't be set to {pair[1]}")


def set_(d: Any, scope: Any, changes: Any) -> dict:
    check_changes(d, scope, changes)
    for op, (old, _) in changes.items():
        if d.permissions.get(scope, op) != old:
            raise ToolError("something was changed meanwhile; reload the page")
    stricter = {op: c for op, c in changes.items() if perms.RANK[c[1]] < perms.RANK[c[0]]}
    looser = {op: c for op, c in changes.items() if op not in stricter}
    approval = d.ask_settings(scope, looser) if looser else None
    if stricter:
        d.permissions.apply(scope, stricter, "hub")
    return {"ok": True, "changed": len(stricter), "approval": approval}


def waiting(d: Any, scope: str) -> dict[str, str]:
    d.approvals.expire()
    for r in d.approvals.recent(50):
        if r["status"] == "pending" and r.get("kind") == "settings.change":
            meta = json.loads(r.get("meta") or "{}")
            if meta.get("scope") == scope:
                return {op: c[1] for op, c in meta["changes"].items()}
    return {}


def ops_rows(d: Any, scope: str) -> list[dict]:
    now, crit, rows = d.permissions.all(scope), critical_ops(d), []
    extra = [f"flow:{f['name']}" for f in flows(d) if f["ok"]] if scope == "chat" else []
    for adapter in (*perms.ADAPTERS, *(perms.OUTSIDE_TOOLS if scope != "chat" else ())):
        mine = sorted(o for o in OPS if o.split(".", 1)[0] == adapter) + (extra if adapter == "checkout" else [])
        items = [{"op": o, "mode": now.get(o, "default"), "modes": perms.allowed_modes(scope, o, OPS, crit),
                  "write": ws.is_write(o) or o in crit, "label": (LABELS.get(o) or OPS[o].label if o in OPS else "Booking script"),
                  "rule": rule(d, scope, o) if o in OPS else "asks once, then runs"} for o in mine]
        rows.append({"adapter": adapter, "name": ADAPTER_NAMES[adapter], "mode": now.get(f"{adapter}.*", "default"),
                     "ops": [i for i in items if i["modes"]],
                     "never": [i["op"] for i in items if not i["modes"]]})
    return rows


def flows(d: Any) -> list[dict]:
    fl, out = d.services.flows, []
    if fl is None or not fl.folder.is_dir():
        return out
    for p in sorted(fl.folder.glob("*.json")):
        try:
            c, _ = ops_flow.load(fl.folder, p.stem)
            out.append({"name": p.stem, "ok": True, "title": c["title"], "site": c["site"], "per_day": c["max_per_day"]})
        except (ToolError, KeyError, ValueError, OSError):
            out.append({"name": p.stem, "ok": False, "title": "can't be read", "site": "", "per_day": 0})
    return out


def overview(d: Any) -> dict:
    s, p, side = d.services, d.policy, d.services.checkout_side
    tg = s.telegram_side
    on = lambda v: "on" if v else "off"  # noqa: E731

    def row(what: str, now: Any, where: str) -> dict:
        return {"what": what, "now": str(now), "where": where}

    accounts = [
        row("Todoist", on(s.todoist_client), "server: /home/ampitools/env"),
        row("Gmail", on(s.gmail_client and s.gmail_client.auth.configured()), "server: ampi-tools gmail-login"),
        row("YouTube", s.youtube_client.status() if s.youtube_client else "off", "server: ampi-tools youtube-login"),
        row("Instagram session", on(s.igweb_client and s.igweb_client.configured()), "server: ampi-tools instagram-session"),
        row("Threads session", on(s.thweb_client and s.thweb_client.configured()), "server: ampi-tools threads-session"),
        row("Phone and saved logins", "see the app", "the companion app; server: ampi-phone"),
    ]
    rules = [
        row("Actions allowed without asking in chat", ", ".join(sorted(p.auto_approve)) or "none", "server: config.toml auto_approve"),
        row("Harmless actions that ask anyway", ", ".join(sorted(p.require_approval)) or "none", "server: config.toml require_approval"),
        row("Your chats and groups: changed here", len(d.permissions.all("chat")), "Abilities"),
    ]
    if tg is not None:
        rules += [row("Telegram chats Ampi may write to", len(tg.rules.allow), "server: config.toml [telegram]"),
                  row("Telegram permissions for 30 days", len(tg.store.grants()), "ask Ampi: telegram permissions"),
                  row("Telegram chats never", len(tg.rules.never), "server: config.toml [telegram]")]
    if side is not None:
        rules += [row("Sites that may be filled without asking", len(side.vault.grants()), "Telegram: /info"),
                  row("Payment limits", f"{side.rules.max_amount} {side.rules.currency} per purchase, {side.rules.max_per_day} a day",
                      "server: config.toml [payment]")]
    auto = [row(f"Watcher {w['name']}", f"{w['status']}, {w['risk']} risk, {w['actions_today']} actions today", "Watchers")
            for w in (d.keeper.list_all() if d.keeper else [])]
    auto += [row(f"Booking script {f['name']}", f["title"], "server: /home/ampitools/flows") for f in flows(d)]
    chat = [row("Tone and character", "", "Telegram: /persona"), row("Answer only when tagged", "", "Telegram: /protect"),
            row("Model", "", "Telegram: /model"), row("Places", "", "Address book"),
            row("Personal info and cards", "", "server: ampi-tools info-set; Telegram: /info"),
            row("Groups and profiles", "", "server: /home/assistant/config.toml")]
    return {"ok": True, "sections": [
        {"title": "Accounts", "rows": accounts}, {"title": "What Ampi may do", "rows": rules},
        {"title": "Runs by itself", "rows": auto}, {"title": "Conversation and data", "rows": chat}]}


def watchers(d: Any) -> list[dict]:
    out = []
    for w in d.keeper.list_all() if d.keeper else []:
        entries = json.loads(w["manifest"] or "[]")
        out.append({"scope": perms.watcher_scope(w["profile"], w["name"]), "name": w["name"], "status": w["status"],
                    "risk": w["risk"], "mode": w["mode"], "description": w["description"], "actions_today": w["actions_today"],
                    "triggers": [ws.trigger_text(e) for e in entries if e.get("kind") == "trigger"],
                    "tools": sorted({t for e in entries if e.get("kind") == "llm" for t in e.get("tools", [])}),
                    "grants": [{"op": g["op"], "step": g["step"], "route": g["route"]} for g in
                               d.keeper.grants(w["profile"], w["name"], w["hash"] or "")]})
    return out


def answer(d: Any, cmd: str, req: dict) -> dict:
    scope = req.get("scope", "chat")
    if cmd == "hub.overview":
        return overview(d)
    if cmd == "hub.set":
        return set_(d, scope, req.get("changes"))
    if cmd == "hub.watchers":
        return {"ok": True, "watchers": watchers(d)}
    if cmd == "hub.abilities":
        if not isinstance(scope, str) or not perms.SCOPE_RE.fullmatch(scope):
            raise ToolError("bad scope")
        return {"ok": True, "scope": scope, "adapters": ops_rows(d, scope), "waiting": waiting(d, scope)}
    if cmd == "hub.activity":
        asked = [{"id": r["id"], "at": r["created"], "status": r["status"], "kind": r.get("kind") or "chat",
                  "summary": d._mask_obj((r["summary"] or "")[:600], "all")}
                 for r in d.approvals.recent(60) if r.get("kind") != "watch.action"
                 and "info." not in (r.get("ops") or "")]
        acts = [{**a, "args_summary": d._mask_obj((a["args_summary"] or "")[:600], "all"),
                 "result": d._mask_obj((a["result"] or "")[:200], "all")} for a in (d.keeper.activity() if d.keeper else [])]
        return {"ok": True, "watchers": acts, "chat": asked, "changes": d.permissions.log(50)}
    raise ToolError("unknown command")
