"""Reactions on the owner's messages: Ampi's own (a `REACT: 👍` line in a reply, taken out of the text) and the status of a
task (seen / done / failed). Telegram lets a bot set one of a fixed list per message."""

from __future__ import annotations

import re

ALLOWED = frozenset(
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 "
    "👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split())
ALIASES = {"✅": "👌", "☑": "👌", "✔": "👌", "⏳": "👀", "⌛": "👀", "🔄": "👨‍💻", "❌": "👎", "📝": "✍", "⭐": "🏆", "❗": "⚡", "⚠": "🤨",
           "💡": "🤓", "❓": "🤔", "🚀": "🔥"}
SEEN, DONE, FAILED = "👀", "👌", "👎"
MAX_PER_REPLY = 10
LINE = re.compile(r"^[ \t]*REACT(?:[ \t]+(\d{1,12}))?[ \t]*:[ \t]*(\S{1,12})[ \t]*$", re.M)
FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
KEY = "react:"

PROMPT = (
    "Each message of the owner starts with its id, e.g. [message 812] (not part of their words). You can put one emoji reaction on "
    "their messages in this conversation: a line `REACT: 👍` in your reply (their latest message) or `REACT 812: 🔥` (that one); a new "
    "one replaces the old, `REACT 812: none` removes it. The lines aren't shown. Use it for status or category, and when a "
    "reaction says enough instead of words (the reply may then be only that line). While you work 👀 is shown, and 👌 after, "
    "unless you choose another. Telegram allows only: " + " ".join(sorted(ALLOWED - {"🖕", "💩", "🤮", "🤬"})) + "\n")


def clean(emoji: str) -> str | None:
    """The emoji as Telegram wants it; "" removes; None if it isn't one of the list."""
    e = emoji.strip("`'\"")
    if e.lower() == "none":
        return ""
    for x in (e, e.replace("️", "")):
        x = ALIASES.get(x, x)
        if x in ALLOWED:
            return x
    return None


def parse(text: str) -> tuple[str, list[tuple[int | None, str]]]:
    """(the text without the lines, [(message id or None = the latest, emoji or "")]). Code blocks stay as they are."""
    code = [m.span() for m in FENCE.finditer(text)]
    found: list[tuple[int | None, str]] = []

    def take(m: re.Match) -> str:
        if any(a <= m.start() < b for a, b in code):
            return m.group(0)
        e = clean(m.group(2))
        if e is not None and len(found) < MAX_PER_REPLY:
            found.append((int(m.group(1)) if m.group(1) else None, e))
        return ""

    out = LINE.sub(take, text)
    return (re.sub(r"\n{3,}", "\n\n", out).strip() if out != text else text), found
