"""The structured part of an approval request: who asks, what for, how risky the ACTION is and why. The bridge shows
it with its own labels; everything that can contain outside text (the action itself, the reason) stays apart."""

from __future__ import annotations

from collections import Counter

from .ops import ALWAYS_CRITICAL, OPS, clean
from .watch_spec import WRITE_OPS

# Short, in the owner's words: what can go wrong and whether it can be undone.
NOTES = {
    "gmail.label": "Changes labels on your email (it may leave the inbox); can be undone",
    "gmail.archive": "Takes email out of the inbox; can be undone",
    "gmail.mark_read": "Marks email as read; can be undone",
    "gmail.mark_unread": "Marks email as unread; can be undone",
    "gmail.star": "Stars email; can be undone",
    "gmail.unstar": "Removes stars; can be undone",
    "gmail.untrash": "Restores email from the trash; can be undone",
    "gmail.draft": "Saves a draft; nothing is sent",
    "gmail.send": "Sends an email as you; can't be undone",
    "gmail.send_draft": "Sends an email as you; can't be undone",
    "gmail.trash": "Moves email to the trash; Gmail deletes it after 30 days",
    "gmail.delete_label": "Deletes a label; can't be undone",
    "todoist.complete": "Completes tasks; can be undone",
    "todoist.delete": "Deletes tasks; can't be undone",
    "telegram.send": "Sends a message as you",
    "telegram.edit": "Changes a message you sent",
    "telegram.delete": "Deletes messages; can't be undone",
    "checkout.fill": "Gives personal details to a website",
    "checkout.pay": "Spends money; can't be undone here",
    "checkout.submit": "Sends or books something on a website in your name; may not be undoable",
    "info.reveal": "Shows personal info to the AI",
    "info.save_place": "Changes your personal info",
    "phone.notify": "Shows a notification on your phone",
    "phone.alarm": "Sets an alarm on your phone; can be undone there",
    "phone.timer": "Sets a timer on your phone; can be undone there",
    "phone.cancel": "Cancels an alarm on your phone: it won't ring",
    "wolt.basket": "Fills a Wolt basket; nothing is ordered",
    "wolt.basket_replace": "Replaces your Wolt basket; nothing is ordered",
}
LABELS = {
    "gmail.label": "Change email labels", "gmail.archive": "Archive email", "gmail.mark_read": "Mark email as read",
    "gmail.mark_unread": "Mark email as unread", "gmail.star": "Star email", "gmail.unstar": "Unstar email",
    "gmail.untrash": "Restore email from trash", "gmail.draft": "Save email draft",
    "todoist.add_project": "Add project", "todoist.add_label": "Add label", "youtube.new_playlist": "New playlist",
}
_ORDER = ("low", "medium", "high")


def level_of(op: str) -> str:
    info = WRITE_OPS.get(op)
    if info is not None:
        return info.level
    return "high" if op in ALWAYS_CRITICAL else "medium"


def note_of(op: str) -> str:
    if op in NOTES:
        return NOTES[op]
    if op.split(".", 1)[0] in ("threads", "instagram", "igweb", "thweb"):
        return "Acts in your name on Instagram/Threads"
    return "Hard or impossible to undo" if level_of(op) == "high" else "Changes something of yours"


def make_card(ops: list[dict], summary: str, who: str, outside: str | None = None, extra: list[str] = ()) -> dict:
    """`summary`: from ops.summarize (a header line, then every action in full). `outside`: why outside content may be
    behind it. Nothing of the actions is left out."""
    names = [o["op"] for o in ops]
    counts = Counter(LABELS.get(n) or OPS[n].label or n for n in names)
    what = ", ".join(label if n == 1 else f"{label} ×{n}" for label, n in counts.items())
    details = summary.split("\n", 1)[1] if "\n" in summary else summary
    if len(ops) == 1:
        details = details.removeprefix("• ")
    risks = list(dict.fromkeys(note_of(n) for n in names))
    if outside:
        risks.append(outside)
    risks += [e for e in extra if e]
    return {"who": clean(who, 120), "what": what, "details": details,
            "level": max((level_of(n) for n in names), key=_ORDER.index), "risks": risks[:8]}
