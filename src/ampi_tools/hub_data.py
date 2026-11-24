"""What the Ampi hub's pages show and change. Runs in the ampi-tools daemon (the only writer); the hub asks over its socket,
checked by user id. Anything looser than now is only requested here: the owner approves it in Telegram."""

from __future__ import annotations

import json
from typing import Any

from . import ops_flow, permissions as perms, watch_spec as ws
from .cards import LABELS
from .ops import ALWAYS_CRITICAL, OPS, clean_block
from .ops_checkout import PRIVATE_ONLY
from .ops_social_session import is_social_request
from .todoist import ToolError

ADAPTER_NAMES = {"todoist": "Todoist", "gmail": "Gmail", "youtube": "YouTube", "threads": "Threads", "instagram": "Instagram",
                 "igweb": "Instagram (your session)", "thweb": "Threads (your session)", "telegram": "Telegram as you",
                 "maps": "Google Maps", "wolt": "Wolt", "info": "Personal info", "checkout": "Checkout browser",
                 "payment": "Payments", "phone": "Your phone", "signin": "Saved logins", "tg-read": "Reading your Telegram",
                 "web": "Web search", "browse": "Web browser"}


def critical_ops(d: Any) -> set[str]:
    return {n for n in OPS if d.policy.critical(n)}


def rule(d: Any, scope: str, op: str) -> str:
    """The built-in rule, in a few words."""
    if perms.kind(scope) == "watcher":
        if op in ws.NOT_FOR_WATCHERS:
            return "never for watchers"
        if not ws.is_write(op):
            return "reads freely"
        if op == "gmail.label":
            return "its AI step labels up to 25 emails freely if you allowed all; moving out of the inbox counts as archiving"
        if op in perms.REPORT_ONLY:
            return "asks, unless allowed for this watcher; \"report\" only changes how you hear of it"
        if op == "gmail.trash":
            return "asks, unless allowed; with \"report\": 25 a request, 50 a day, new unimportant mail, never about security"
        return "asks, unless allowed for this watcher"
    kind = op.split(".", 1)[0]
    if op in PRIVATE_ONLY or kind == "signin":
        return "your private chat only; " + ("asks" if d.policy.critical(op) else "doesn't ask by itself")
    if kind == "telegram" and ws.is_write(op):
        return "asks, unless you allowed the chat"
    if kind in ("igweb", "thweb"):
        return "social conversations only; changes ask"
    if op in ("youtube.ids", "youtube.show", "gmail.meta"):
        return "Ampi's own checks only"
    if op == "gmail.download":
        return "your private chat only"
    if kind == "wolt" and ws.is_write(op):
        return "your chat freely; elsewhere asks"
    if op in ("phone.status", "phone.alarms", "phone.cancel"):
        return "your private chat only"
    if kind == "phone":
        return "your chat freely; groups ask"
    if op in ALWAYS_CRITICAL:
        return "always asks"
    if d.policy.critical(op):
        return "asks"
    if ws.is_write(op):
        return "does it" if kind == "gmail" else "does it; asks when others would see it"
    return "reads freely"


def check_scope(d: Any, scope: Any) -> None:
    if not isinstance(scope, str) or not perms.SCOPE_RE.fullmatch(scope):
        raise ToolError("bad scope")
    if perms.kind(scope) == "watcher":
        profile, name = scope.removeprefix("watcher:").split("/", 1)
        w = d.keeper.watcher(profile, name) if d.keeper else None
        if not w or w["status"] in ("deleted", "proposed"):
            raise ToolError("no such watcher")
    elif perms.kind(scope) == "topic" and not any(t["scope"] == scope for t in d.permissions.topics()):
        raise ToolError("no such topic (write something there first)")


def check_role(role: Any) -> str:
    if not isinstance(role, str) or len(role) > perms.MAX_ROLE:
        raise ToolError(f"a role is text of at most {perms.MAX_ROLE} characters")
    lines = clean_block(role.strip()).split("\n") if role.strip() else []
    return "\n".join(x.removeprefix("│ ") for x in lines)


def extra_lines(d: Any, extra: dict) -> list[str]:
    out = []
    if "role" in extra:
        out.append("• Role (instructions for Ampi there):\n" + (clean_block(extra["role"][1]) if extra["role"][1] else "(none)"))
    if extra.get("resume"):
        out.append("• Turn on again: " + ", ".join(s.split("/", 1)[1] for s in extra["resume"]))
    if extra.get("deliver"):
        out.append(f"• Its messages and requests go to: {perms.words(extra['deliver'][1])}")
    return out


def check_extra(d: Any, scope: str, extra: dict) -> None:
    if set(extra) - {"role", "resume", "deliver", "unstop"}:
        raise ToolError("bad request")
    if "role" in extra:
        check_scope(d, scope)
        old, new = extra["role"]
        if perms.kind(scope) != "topic" or check_role(new) != new:
            raise ToolError("bad role")
        if d.permissions.role(scope) != old:
            raise ToolError("the role was changed meanwhile; nothing was changed now")
    for s in extra.get("resume") or []:
        check_scope(d, s)
    if extra.get("deliver"):
        check_scope(d, scope)
        old, new = extra["deliver"]
        check_delivery(scope, new)
        if d.permissions.delivery(scope) != old:
            raise ToolError("that was changed meanwhile; nothing was changed now")


def check_delivery(scope: str, topic: Any) -> None:
    where = perms.private_thread(topic) if isinstance(topic, str) else None
    if perms.kind(scope) != "watcher" or not where or where[0] != scope.removeprefix("watcher:").split("/", 1)[0]:
        raise ToolError("a watcher's messages go to a topic of its own private chat")


def apply_extra(d: Any, scope: str, extra: dict, how: str) -> None:
    if "role" in extra:
        d.permissions.set_role(scope, extra["role"][1], how)
    for s in extra.get("resume") or []:
        profile, name = s.removeprefix("watcher:").split("/", 1)
        if (d.keeper.watcher(profile, name) or {}).get("status") in ("paused", "failing"):
            d.keeper.set_status(profile, name, "active")
    if extra.get("unstop"):
        d.permissions.remember("stopped", None)
    if extra.get("deliver"):
        d.permissions.set_delivery(scope, extra["deliver"][1], how)


def check_changes(d: Any, scope: str, changes: Any) -> None:
    check_scope(d, scope)
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
                return {op: c[1] for op, c in meta["changes"].items()} | (
                    {"role": meta["extra"]["role"][1] or "(none)"} if "role" in (meta.get("extra") or {}) else {})
    return {}


def pending_extra(d: Any, key: str) -> dict[str, Any]:
    """scope -> what waits for approval there."""
    out = {}
    for r in d.approvals.recent(100):
        if r["status"] == "pending" and r.get("kind") == "settings.change":
            meta = json.loads(r.get("meta") or "{}")
            if (meta.get("extra") or {}).get(key):
                out[meta["scope"]] = meta["extra"][key]
    return out


def ops_rows(d: Any, scope: str) -> list[dict]:
    now, crit, rows = d.permissions.all(scope), critical_ops(d), []
    watcher = perms.kind(scope) == "watcher"
    extra = [] if watcher else [f"flow:{f['name']}" for f in flows(d) if f["ok"]]
    for adapter in (*perms.ADAPTERS, *(k for k in perms.OUTSIDE_TOOLS if scope != "chat" and perms.allowed_modes(scope, f"{k}.*", OPS, crit))):
        mine = sorted(o for o in OPS if o.split(".", 1)[0] == adapter) + (extra if adapter == "checkout" else [])
        items = [{"op": o, "mode": now.get(o, "default"), "modes": perms.allowed_modes(scope, o, OPS, crit),
                  "write": ws.is_write(o) or o in crit or o in perms.CHANGES,
                  "now": d.permissions.mode(scope, o, {"flow": o[5:]} if o.startswith("flow:") else {}) if o in OPS else None, "label": (LABELS.get(o) or OPS[o].label if o in OPS else "Booking script"),
                  "rule": rule(d, scope, o) if o in OPS else "asks once, then runs"} for o in mine]
        rows.append({"adapter": adapter, "name": ADAPTER_NAMES[adapter], "mode": now.get(f"{adapter}.*", "default"),
                     "modes": perms.allowed_modes(scope, f"{adapter}.*", OPS, crit),
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
                    "risk": w["risk"], "mode": w["mode"], "last": w.get("last"),
                    "deliver_waiting": (pending_extra(d, "deliver").get(perms.watcher_scope(w["profile"], w["name"])) or ["", ""])[1],
                    "resume_waiting": any(perms.watcher_scope(w["profile"], w["name"]) in x for x in pending_extra(d, "resume").values()),
                    "deliver": d.permissions.delivery(perms.watcher_scope(w["profile"], w["name"])),
                    "limits": d._limits_of(w["hash"]) if w["hash"] else {}, "description": w["description"], "actions_today": w["actions_today"],
                    "triggers": [ws.trigger_text(e) for e in entries if e.get("kind") == "trigger"],
                    "tools": sorted({t for e in entries if e.get("kind") == "llm" for t in e.get("tools", [])}),
                    "grants": [{"op": g["op"], "step": g["step"], "route": g["route"]} for g in
                               d.keeper.grants(w["profile"], w["name"], w["hash"] or "")]})
    return out


def topics(d: Any) -> list[dict]:
    p = d.permissions
    going = {}
    for w in d.keeper.list_all() if d.keeper else []:
        s = perms.watcher_scope(w["profile"], w["name"])
        if p.delivery(s):
            going.setdefault(p.delivery(s), []).append(w["name"])
    return [{"scope": t["scope"], "title": t["title"], "where": perms.words(t["scope"]), "role": t["role"], "seen": t["seen"],
             "set": len(p.all(t["scope"])), "changes": p.get(t["scope"], "changes.*"), "off": p.off(t["scope"]),
             "watchers": going.get(t["scope"], []), "private": perms.private_thread(t["scope"]) is not None,
             "waiting": waiting(d, t["scope"])} for t in p.topics()]


def pending(d: Any) -> list[dict]:
    d.approvals.expire()
    return [{"id": r["id"], "at": r["created"], "expires": r.get("expires"), "kind": r.get("kind") or "chat",
             "summary": d._mask_obj((r["summary"] or "")[:400], "all")}
            for r in d.approvals.recent(200) if r["status"] == "pending" and "info." not in (r.get("ops") or "")
            and not is_social_request(r)]


def home(d: Any) -> dict:
    ws_ = watchers(d)
    k = d.keeper
    today = k.today() if k else {}
    return {"ok": True, "pending": pending(d), "stopped": bool(d.permissions.remembered("stopped")), "changes": d.permissions.get("chat", "changes.*"),
            "watchers": [{x: w[x] for x in ("scope", "name", "status", "actions_today", "last")} for w in ws_],
            "today": today, "recent": activity_rows(d, 6),
            "limits": {"chat": len(d.permissions.all("chat")), "topics": sum(1 for t in topics(d) if t["set"] or t["role"])}}


def given(d: Any) -> dict:
    """Standing permissions, each with what removes it."""
    out = []
    tg, side = d.services.telegram_side, d.services.checkout_side
    for g in tg.store.grants() if tg is not None else []:
        out.append({"kind": "telegram", "key": [g["peer_id"], g["action"]], "what": f"{g['action']} in {g['title']} as you",
                    "until": g["expires"]})
    for g in side.vault.grants() if side is not None else []:
        out.append({"kind": "site", "key": [g["origin"], g["fields"]], "what": f"fill {g['fields']} on {g['origin']}",
                    "until": g["expires"]})
    for w in d.keeper.list_all() if d.keeper else []:
        for g in d.keeper.grants(w["profile"], w["name"], w["hash"] or ""):
            to = " / ".join(str(v) for v in g["route"].values() if v not in (None, ""))
            out.append({"kind": "watcher", "key": [g["id"]], "what": f"watcher {w['name']}: {g['op']}" + (f" → {to}" if to else ""),
                        "until": None, "source": g["source"]})
    return {"ok": True, "given": out}


def revoke(d: Any, kind: Any, key: Any) -> dict:
    ok = isinstance(key, list) and all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in key)
    n = 0
    if kind == "telegram" and ok and len(key) == 2 and isinstance(key[0], int) and d.services.telegram_side is not None:
        n = d.services.telegram_side.store.revoke(key[0], str(key[1]))
    elif kind == "site" and ok and len(key) == 2 and d.services.checkout_side is not None:
        db = d.services.checkout_side.vault.db
        n = db.execute("DELETE FROM grants WHERE origin=? AND fields=?", (str(key[0]), str(key[1]))).rowcount
        db.commit()
    elif kind == "watcher" and ok and len(key) == 1 and isinstance(key[0], int) and d.keeper is not None:
        n = d.keeper.remove_grant(key[0])
    else:
        raise ToolError("bad request")
    if not n:
        raise ToolError("it was gone already; reload the page")
    return {"ok": True, "changed": n}


def pause(d: Any, scopes: Any) -> int:
    if not isinstance(scopes, list) or not 1 <= len(scopes):
        raise ToolError("which watchers?")
    n = 0
    for s in scopes:
        check_scope(d, s)
        if perms.kind(s) != "watcher":
            raise ToolError("not a watcher")
        profile, name = s.removeprefix("watcher:").split("/", 1)
        if d.keeper.watcher(profile, name)["status"] in ("active", "failing"):
            d.keeper.set_status(profile, name, "paused")
            n += 1
    return n


def stop(d: Any) -> dict:
    """Nothing is changed by Ampi any more: every watcher paused, every change in chats and groups off."""
    running = [perms.watcher_scope(w["profile"], w["name"]) for w in (d.keeper.list_all() if d.keeper else [])
               if w["status"] in ("active", "failing")]
    now = d.permissions.get("chat", "changes.*")
    before = d.permissions.remembered("stopped")
    d.permissions.remember("stopped", {"mode": before["mode"] if before else now,
                                       "watchers": sorted({*(before["watchers"] if before else ()), *running})})
    if now != "off":
        d.permissions.apply("chat", {"changes.*": [now, "off"]}, "hub: stop everything")
    n = pause(d, running) if running else 0
    for r in d.approvals.recent(500):  # nothing asked before may turn something on again
        if r["status"] == "pending" and r.get("kind") == "settings.change":
            d.approvals.claim(r["id"], "expired")
    return {"ok": True, "changed": n + (now != "off"), "paused": n}


def resume(d: Any, scopes: Any, everything: bool) -> dict:
    changes: dict = {}
    if everything:
        was = d.permissions.remembered("stopped")
        if not was:
            raise ToolError("nothing is stopped; reload the page")
        scopes = was["watchers"]
        if d.permissions.get("chat", "changes.*") == "off" and was["mode"] != "off":
            changes = {"changes.*": ["off", was["mode"]]}
    if not isinstance(scopes, list):
        raise ToolError("which watchers?")
    again = []
    for s in scopes:
        try:
            check_scope(d, s)
        except ToolError:
            continue  # deleted meanwhile
        profile, name = s.removeprefix("watcher:").split("/", 1)
        if perms.kind(s) == "watcher" and d.keeper.watcher(profile, name)["status"] in ("paused", "failing"):
            again.append(s)
    if not again and not changes:
        if everything:  # nothing is left to loosen: only the note goes
            d.permissions.remember("stopped", None)
            return {"ok": True, "changed": 1}
        raise ToolError("nothing to turn on")
    extra = {**({"resume": again} if again else {}), **({"unstop": True} if everything else {})}
    return {"ok": True, "changed": 0, "approval": d.ask_settings("chat", changes, extra)}


def activity_rows(d: Any, limit: int, before: Any = None, name: Any = None, op: Any = None, status: Any = None) -> list[dict]:
    rows = d.keeper.activity(limit, before if isinstance(before, int) else None, *(x if isinstance(x, str) and x else None
                                                                                  for x in (name, op, status))) if d.keeper else []
    return [{**{k: a[k] for k in ("id", "name", "op", "status", "at", "items", "approval_id")},
             "label": LABELS.get(a["op"]) or a["op"],
             "args_summary": d._mask_obj((a["args_summary"] or "")[:600], "all"),
             "result": d._mask_obj((a["result"] or "")[:200], "all"),
             "detail": d._mask_obj([f"{i['subject']} · {i['who']}" for i in (_json(a["detail"]) or {}).get("items", [])], "all")}
            for a in rows]


def _json(raw: Any) -> dict | None:
    try:
        v = json.loads(raw or "null")
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def answer(d: Any, cmd: str, req: dict) -> dict:
    scope = req.get("scope", "chat")
    if cmd == "hub.overview":
        return overview(d)
    if cmd == "hub.home":
        return home(d)
    if cmd == "hub.topics":
        return {"ok": True, "topics": topics(d)}
    if cmd == "hub.given":
        return given(d)
    if cmd == "hub.revoke":
        return revoke(d, req.get("kind"), req.get("key"))
    if cmd == "hub.pause":
        return {"ok": True, "changed": pause(d, req.get("scopes"))}
    if cmd == "hub.resume":
        return resume(d, req.get("scopes"), req.get("everything") is True)
    if cmd == "hub.stop":
        return stop(d)
    if cmd == "hub.role":
        check_scope(d, scope)
        if perms.kind(scope) != "topic":
            raise ToolError("a role belongs to a topic")
        role = check_role(req.get("role"))
        if role == d.permissions.role(scope):
            raise ToolError("that is the role now")
        return {"ok": True, "changed": 0, "approval": d.ask_settings(scope, {}, {"role": [d.permissions.role(scope), role]})}
    if cmd == "hub.forget":
        check_scope(d, scope)
        if perms.kind(scope) != "topic" or d.permissions.all(scope) or d.permissions.role(scope) \
                or any(t["scope"] == scope and t["watchers"] for t in topics(d)):
            raise ToolError("only a topic without role, limits and watchers can be forgotten")
        d.permissions.forget_topic(scope)
        return {"ok": True, "changed": 1}
    if cmd == "hub.deliver":
        check_scope(d, scope)
        topic = req.get("topic") or ""
        if topic:  # (where requests and reports appear: the owner should know)
            extra = {"deliver": [d.permissions.delivery(scope), topic]}
            check_extra(d, scope, extra)
            check_scope(d, topic)
            return {"ok": True, "changed": 0, "approval": d.ask_settings(scope, {}, extra)}
        for r in d.approvals.recent(100):
            if r["status"] == "pending" and r.get("kind") == "settings.change":
                m = json.loads(r.get("meta") or "{}")
                if m.get("scope") == scope and (m.get("extra") or {}).get("deliver") and not m.get("changes"):
                    d.approvals.claim(r["id"], "expired")
        d.permissions.set_delivery(scope, "", "hub")
        return {"ok": True, "changed": 1}
    if cmd == "hub.set":
        return set_(d, scope, req.get("changes"))
    if cmd == "hub.watchers":
        return {"ok": True, "watchers": watchers(d)}
    if cmd == "hub.abilities":
        if not isinstance(scope, str) or not perms.SCOPE_RE.fullmatch(scope):
            raise ToolError("bad scope")
        return {"ok": True, "scope": scope, "changes": d.permissions.get(scope, "changes.*"), "adapters": ops_rows(d, scope), "waiting": waiting(d, scope)}
    if cmd == "hub.activity":
        asked = [{"id": r["id"], "at": r["created"], "status": r["status"], "kind": r.get("kind") or "chat",
                  "summary": d._mask_obj((r["summary"] or "")[:600], "all")}
                 for r in d.approvals.recent(60) if r.get("kind") != "watch.action"
                 and "info." not in (r.get("ops") or "") and not is_social_request(r)]
        acts = activity_rows(d, 60, req.get("before"), req.get("name"), req.get("op"), req.get("status"))
        return {"ok": True, "watchers": acts, "chat": asked, "changes": d.permissions.log(50), "pending": pending(d)}
    raise ToolError("unknown command")
