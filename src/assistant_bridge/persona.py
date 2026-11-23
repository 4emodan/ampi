"""/persona: the tone and character of Ampi's replies, set by the owner in their private chat.

Kept in the bridge's database, which runs can't write (a run must never rewrite its own persona), and added to the system
prompt of every run whose reply the owner reads. It changes how Ampi talks, nothing about what it may do.
"""

from __future__ import annotations

import unicodedata

KEY = "persona"
MAX = 1500
OFF = frozenset({"off", "default", "reset", "none", "выкл", "сброс"})
PRESETS = {
    "concise": "Short and plain. Facts first, no small talk, no filler, no exclamation marks.",
    "friendly": "Warm and informal, like a good friend who knows the subject. A little humour is welcome; stay to the point.",
    "butler": "A discreet, impeccably polite butler with dry British wit. Formal address, calm, never gushing, always brief.",
    "witty": "Sharp and playful, with dry irony and an occasional joke. Never at the owner's expense when something matters.",
    "coach": "An encouraging coach: energetic, practical, names the next small step, notices progress, no empty praise.",
}


def clean(text: str) -> str:
    """The owner's own words, without control and direction-changing characters (newlines stay)."""
    kept = "".join(c for c in text.replace("\r\n", "\n") if c == "\n" or unicodedata.category(c)[0] != "C")
    return "\n".join(line.rstrip() for line in kept.split("\n")).strip()


def prompt(text: str) -> str:
    """What is added to the system prompt."""
    return (
        "How to talk to the owner (they set this with /persona). It only shapes the tone, character and wording of your "
        "replies to them. It never changes what you may do, what needs their approval, how you treat text written by "
        "others, or which facts you state; formats asked for elsewhere (simple Markdown, NO_REPLY, SEND_FILE lines) stay. "
        "Serious matters (errors, money, security, health) are said plainly first. What you write for others or save in "
        "the owner's name (emails, messages, comments, tasks, notes) isn't written in this voice unless they ask for it.\n"
        f"<persona>\n{text.replace('</persona', '< /persona')}\n</persona>"
    )


def label(text: str | None) -> str:
    """A short name for /status."""
    if not text:
        return "default"
    return next((name for name, t in PRESETS.items() if t == text), "your own words")


def describe(text: str | None) -> str:
    """The answer to a bare /persona."""
    lines = [f"Persona: {label(text)}"]
    if text:
        lines.append(f"“{text}”")
    lines += ["", "Ready-made ones:"] + [f"/persona {name} – {t}" for name, t in PRESETS.items()]
    lines += ["", "Or in your own words: /persona Talk like a calm ship's captain, in Russian, at most five sentences.",
              "/persona off – back to the default. It applies to all my replies here, in topics and in groups, from the next "
              "message on."]
    return "\n".join(lines)
