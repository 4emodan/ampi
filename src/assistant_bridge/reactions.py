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
LINE = re.compile(r"[ \t]*REACT(?:[ \t]+(\d{1,12}))?[ \t]*:[ \t]*(\S{1,12})[ \t]*")
CONTROL = re.compile(r"[ \t]*(?:SEND_FILE:.+|NO_REPLY)[ \t]*")
KEY = "react:"

PROMPT = (
    "Each message of the owner starts with its id, e.g. [message 812] (not part of their words). You can put one emoji reaction on "
    "their messages in this conversation: a line `REACT: 👍` in your reply (their latest message) or `REACT 812: 🔥` (that one); a new "
    "one replaces the old, `REACT 812: none` removes it. Put these lines at the very end of the reply; they aren't shown. Use it for status or category, and when a "
    "reaction says enough instead of words (the reply may then be only that line). In private chats 👀 is shown while you work and "
    "👌 after, unless you choose another. Telegram allows only: " + " ".join(sorted(ALLOWED - {"🖕", "💩", "🤮", "🤬"})) + "\n")


def clean(emoji: str) -> str | None:
    """The emoji as Telegram wants it; "" removes; None if it isn't one of the list."""
    e = emoji.strip("`'\"")
    if e.lower() == "none":
        return ""
    e = re.sub("[🏻-🏿]", "", e)  # (no skin tones there)
    for x in (e, e.replace("️", "")):
        x = ALIASES.get(x, x)
        if x in ALLOWED:
            return x
    return None


def parse(text: str) -> tuple[str, list[tuple[int | None, str]]]:
    """(the text without its last REACT lines, [(message id or None = the latest, emoji or "")]). Only at the very end, so a quoted
    email or a code block in the middle means nothing."""
    lines, found, other, kept = text.rstrip().split("\n"), [], [], []
    while lines and ((m := LINE.fullmatch(lines[-1].strip().strip("`"))) or CONTROL.fullmatch(lines[-1])):
        if not m:  # the bridge's own lines may stand between
            kept.insert(0, lines.pop())
            continue
        lines.pop()
        e = clean(m.group(2))
        if e is not None:
            found.append((int(m.group(1)) if m.group(1) else None, e))
        else:
            other.append(m.group(2))
        while lines and not lines[-1].strip():
            lines.pop()
    rest = "\n".join(lines)
    if not (found or other) or rest.count("```") % 2:  # (inside an unclosed code block)
        return text, []
    if not rest.strip() and not found and not kept:
        rest = " ".join(other[::-1])  # an emoji Telegram doesn't offer as a reaction: then as text
    return "\n".join([rest.strip(), *kept]).strip(), found[::-1][:MAX_PER_REPLY]
