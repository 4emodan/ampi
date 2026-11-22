"""Turns HTML into readable plain text (drops scripts, styles and tags; keeps line breaks and link targets).

Args:
  field  where the HTML is (default "input")
  links  true: keep link targets as "text (url)" (default true)
  limit  maximum characters returned (default 20000)
Returns: {"text": "..."}
"""

import html
import json
import re
import sys
from html.parser import HTMLParser


def get(req, path, default=None):
    cur = req
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
        if cur is None:
            return default
    return cur


BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}


class Text(HTMLParser):
    def __init__(self, links):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.links, self.href = [], 0, links, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        elif tag in BLOCK:
            self.out.append("\n")
        elif tag == "a" and self.links:
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self.out.append("\n")
        elif tag == "a" and self.href:
            if self.href.startswith(("http://", "https://")):
                self.out.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def main(req):
    args = req.get("args") or {}
    raw = str(get(req, args.get("field", "input"), "") or "")
    p = Text(args.get("links", True))
    p.feed(raw)
    text = html.unescape("".join(p.out))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()
    return {"text": text[: int(args.get("limit", 20000))]}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
