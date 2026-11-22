"""Finds prices in text: "€12.50", "12,50 EUR", "1 299 kr", "$9.99".

Args:
  field     where the text is (default "input")
  currency  keep only this currency (e.g. "EUR")
Returns: {"prices": [{"amount": 12.5, "currency": "EUR", "text": "12,50 €"}], "lowest": 12.5 | null}
"""

import json
import re
import sys

SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "kr": "SEK", "zł": "PLN", "₽": "RUB", "¥": "JPY", "₹": "INR"}
CODES = r"EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|RUB|JPY|INR|AED|TRY"
SYM = r"€|\$|£|kr|zł|₽|¥|₹"
NUM = r"\d{1,3}(?:[  .,']\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
PATTERN = re.compile(rf"(?P<pre>{CODES}|{SYM})\s?(?P<a>{NUM})|(?P<b>{NUM})\s?(?P<post>{CODES}|{SYM})", re.I)


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


def number(s):
    """'1 299,50' -> 1299.5, '1,299' -> 1299 (3 digits after a separator = thousands), '12.5' -> 12.5."""
    s = s.replace(" ", "").replace("'", "").replace(" ", "")
    m = re.fullmatch(r"(.*?)[.,](\d{1,2})", s)
    whole, dec = (m.group(1), m.group(2)) if m else (s, "0")
    return float(re.sub(r"[.,]", "", whole or "0") + "." + dec)


def main(req):
    args = req.get("args") or {}
    value = get(req, args.get("field", "input"), "")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    want = (args.get("currency") or "").upper()
    prices = []
    for m in PATTERN.finditer(text):
        cur = (m.group("pre") or m.group("post") or "")
        cur = SYMBOLS.get(cur.lower() if cur.lower() in ("kr", "zł") else cur, cur.upper())
        try:
            amount = number(m.group("a") or m.group("b"))
        except ValueError:
            continue
        if want and cur != want:
            continue
        prices.append({"amount": amount, "currency": cur, "text": m.group(0).strip()})
    return {"prices": prices[:100], "lowest": min((p["amount"] for p in prices), default=None)}


if __name__ == "__main__":
    json.dump(main(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
