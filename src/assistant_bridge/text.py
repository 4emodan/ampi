"""Turning Claude's Markdown replies into Telegram messages."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096
CHUNK_LIMIT = 3800  # leaves room for re-opened code fences

_FENCE_LINE = re.compile(r"^\s*```([\w+#.-]*)\s*$")
_FENCE_BLOCK = re.compile(r"```([\w+#.-]*)[ \t]*\n(.*?)```", re.S)


def split_markdown(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into chunks under `limit`, preferring line breaks and keeping code fences balanced."""
    text = text.strip()
    if not text:
        return []
    budget = max(limit - 40, 100)

    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > budget:
            cut = line.rfind(" ", 0, budget)
            cut = cut if cut > budget // 2 else budget
            lines.append(line[:cut])
            line = line[cut:].lstrip(" ")
        lines.append(line)

    raw_chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        extra = len(line) + (1 if current else 0)
        if current and size + extra > budget:
            raw_chunks.append("\n".join(current))
            current, size = [], 0
            extra = len(line)
        current.append(line)
        size += extra
    if current:
        raw_chunks.append("\n".join(current))

    chunks: list[str] = []
    open_lang: str | None = None
    for chunk in raw_chunks:
        if open_lang is not None:
            chunk = f"```{open_lang}\n{chunk}"
        state: str | None = None
        for line in chunk.split("\n"):
            m = _FENCE_LINE.match(line)
            if m:
                state = m.group(1) if state is None else None
        if state is not None:
            chunk += "\n```"
        open_lang = state
        chunk = chunk.strip("\n")
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def markdown_to_html(md: str) -> str:
    """Convert common Markdown to Telegram's HTML subset. Unsupported syntax stays as text."""
    out: list[str] = []
    pos = 0
    for m in _FENCE_BLOCK.finditer(md):
        out.append(_blocks(md[pos : m.start()]))
        lang, code = m.group(1), m.group(2).rstrip("\n")
        cls = f' class="language-{html.escape(lang)}"' if lang else ""
        out.append(f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>")
        pos = m.end()
    out.append(_blocks(md[pos:]))
    return "".join(out).strip()


def _blocks(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            lines.append(f"<b>{_inline(heading.group(1))}</b>")
            continue
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            lines.append("──────────")
            continue
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            lines.append(f"{bullet.group(1)}• {_inline(bullet.group(2))}")
            continue
        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            lines.append(f"▎{_inline(quote.group(1))}")
            continue
        lines.append(_inline(line))
    return "\n".join(lines)


def _inline(s: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    s = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), s)
    s = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        lambda m: keep(
            f'<a href="{html.escape(m.group(2), quote=True)}">{_emphasis(html.escape(m.group(1), quote=False))}</a>'
        ),
        s,
    )
    s = _emphasis(html.escape(s, quote=False))
    while "\x00" in s:
        s = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)
    return s


def _emphasis(s: str) -> str:
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", s)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", s)
    return s
