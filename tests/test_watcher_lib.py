"""The shared watcher helper scripts (templates/watcher-lib), run the way the sandbox runs them: cwd with space/."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "templates" / "watcher-lib"


def lib(name):
    spec = importlib.util.spec_from_file_location(f"watcherlib_{name}", LIB / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def cwd(tmp_path, monkeypatch):
    (tmp_path / "space").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def space(name):
    return Path("space") / name


def test_every_lib_script_has_a_docstring_with_args_and_returns():
    for f in LIB.glob("*.py"):
        doc = lib(f.stem).__doc__
        assert doc and "Returns" in doc, f.name


def test_match_sender_patterns_and_list():
    m = lib("match_sender")
    item = {"from": "Office <Office@Pascal-School.edu>"}
    assert m.main({"item": item, "args": {"patterns": ["pascal"]}}) == {
        "matched": True, "address": "office@pascal-school.edu", "why": "pattern pascal"}
    out = m.main({"item": {"from": "Anna <anna@gmail.com>"}, "args": {"patterns": ["pascal"]}})
    assert out["matched"] is False and out["stop"] is True
    space("known.txt").write_text("# teachers\nanna@gmail.com\n@school.org\n", encoding="utf-8")
    assert m.main({"item": {"from": "anna@gmail.com"}, "args": {"list": "known.txt"}})["why"] == "list"
    assert m.main({"item": {"from": "x@school.org"}, "args": {"list": "space/known.txt"}})["matched"]
    assert m.main({"item": {"from": "x@other.org"}, "args": {"list": "missing.txt", "stop": False}}) == {
        "matched": False, "address": "x@other.org", "why": ""}
    with pytest.raises(SystemExit):
        m.main({"item": item, "args": {"list": "../etc/passwd"}})


def test_match_text():
    m = lib("match_text")
    req = {"item": {"subject": "Tickets on SALE now: 25.12"}}
    assert m.main({**req, "args": {"field": "item.subject", "any": ["sale", "drop"]}})["matched"]
    assert m.main({**req, "args": {"field": "item.subject", "all": ["sale", "drop"]}})["stop"]
    assert not m.main({**req, "args": {"field": "item.subject", "any": ["sale"], "none": ["tickets"]}})["matched"]
    out = m.main({**req, "args": {"field": "item.subject", "regex": r"(\d+)\.(\d+)"}})
    assert out["matched"] and out["groups"] == ["25", "12"]


def test_dedupe():
    d = lib("dedupe")
    assert d.main({"item": {"id": "a"}, "args": {}}) == {"new": True, "key": "a"}
    assert d.main({"item": {"id": "a"}, "args": {}}) == {"new": False, "key": "a", "stop": True}
    assert d.main({"item": {"id": "b"}, "args": {"keep": 1}})["new"]
    assert space("seen.txt").read_text().split() == ["b"]
    assert d.main({"steps": {"x": {"o": {"k": 1}}}, "args": {"field": "steps.x.o"}})["key"] == '{"k": 1}'


def test_html_to_text():
    h = lib("html_to_text")
    html = ("<html><head><style>p{}</style><script>alert(1)</script></head><body><h1>Title</h1>"
            "<p>Hello &amp; <a href='https://x.org/a'>link</a></p><p>Second</p></body></html>")
    text = h.main({"input": html, "args": {}})["text"]
    assert "alert" not in text and "p{}" not in text
    assert text.startswith("Title") and "Hello & link (https://x.org/a)" in text and "Second" in text
    assert h.main({"input": html, "args": {"links": False, "limit": 5}})["text"] == "Title"


def test_extract_links():
    e = lib("extract_links")
    text = "See https://tickets.example.com/e/1, and http://evil.com/x. Also https://sub.tickets.example.com/2)"
    out = e.main({"input": text, "args": {"domains": ["tickets.example.com"]}})
    assert out["links"] == ["https://tickets.example.com/e/1", "https://sub.tickets.example.com/2"]
    assert e.main({"input": text, "args": {"contains": ["evil"]}})["links"] == ["http://evil.com/x"]


@pytest.mark.parametrize("text,amount,cur", [
    ("Now only €12.50!", 12.5, "EUR"),
    ("12,50 EUR", 12.5, "EUR"),
    ("1 299 kr", 1299.0, "SEK"),
    ("$9.99", 9.99, "USD"),
    ("1.299,00 €", 1299.0, "EUR"),
    ("USD 1,299", 1299.0, "USD"),
    ("£5", 5.0, "GBP"),
])
def test_parse_price(text, amount, cur):
    out = lib("parse_price").main({"input": text, "args": {}})
    assert out["prices"][0]["amount"] == amount and out["prices"][0]["currency"] == cur


def test_parse_price_lowest_and_currency_filter():
    p = lib("parse_price")
    out = p.main({"input": "was €30, now €19.90 or $25", "args": {"currency": "EUR"}})
    assert [x["amount"] for x in out["prices"]] == [30.0, 19.9] and out["lowest"] == 19.9
    assert p.main({"input": "no prices here", "args": {}}) == {"prices": [], "lowest": None}


def test_compare_number():
    c = lib("compare_number")
    req = {"steps": {"p": {"lowest": 19.9}}, "args": {"field": "steps.p.lowest", "op": "<", "threshold": 20}}
    assert c.main(req)["passed"]
    req["args"]["threshold"] = 10
    assert c.main(req)["stop"]
    drop = {"field": "steps.p.lowest", "op": "dropped"}
    first = c.main({"steps": {"p": {"lowest": 30}}, "args": drop})
    assert first["passed"] is False and first["previous"] is None
    second = c.main({"steps": {"p": {"lowest": 25}}, "args": drop})
    assert second == {"passed": True, "value": 25.0, "previous": 30.0}
    assert c.main({"steps": {"p": {"lowest": 26}}, "args": drop})["stop"]
    assert c.main({"steps": {}, "args": {"field": "steps.p.lowest", "op": "<", "threshold": 1}})["passed"] is False


def test_diff_text():
    d = lib("diff_text")
    first = d.main({"input": "a\nb\nUpdated 10:00", "args": {"ignore": [r"^Updated"]}})
    assert first["first"] and not first["changed"] and first["stop"]
    same = d.main({"input": "a\nb\nUpdated 10:05", "args": {"ignore": [r"^Updated"]}})
    assert not same["changed"]
    new = d.main({"input": "a\nc\nUpdated 10:10", "args": {"ignore": [r"^Updated"]}})
    assert new["changed"] and new["added"] == ["c"] and new["removed"] == ["b"] and "stop" not in new


def test_pick():
    p = lib("pick")
    req = {"item": {"subject": "S"}, "steps": {"l": {"summary": "x"}},
           "args": {"fields": {"s": "item.subject", "sum": "steps.l.summary", "m": "item.none"},
                    "defaults": {"m": "-"}}}
    assert p.main(req) == {"s": "S", "sum": "x", "m": "-"}


def test_digest_add_and_flush():
    d = lib("digest")
    args = {"fields": {"subject": "item.subject"}}
    assert d.main({"item": {"subject": "one"}, "args": args}) == {"count": 1, "stop": True}
    assert d.main({"item": {"subject": "two"}, "args": {**args, "stop": False}}) == {"count": 2}
    out = d.main({"args": {"mode": "flush"}})
    assert out["items"] == [{"subject": "one"}, {"subject": "two"}] and out["count"] == 2
    assert d.main({"args": {"mode": "flush"}})["stop"]


def test_list_file():
    lf = lib("list_file")
    assert lf.main({"args": {"file": "a.txt"}}) == {"entries": []}
    assert lf.main({"args": {"file": "a.txt", "op": "add", "value": "X@y.org"}}) == {"changed": True, "count": 1}
    assert lf.main({"args": {"file": "a.txt", "op": "add", "value": "x@Y.org"}})["changed"] is False
    assert lf.main({"steps": {"s": {"address": "x@y.org"}}, "args": {"file": "a.txt", "op": "has",
                                                                       "field": "steps.s.address"}}) == {"has": True}
    assert lf.main({"args": {"file": "a.txt", "op": "has", "value": "z", "stop": True}}) == {"has": False, "stop": True}
    assert lf.main({"args": {"file": "a.txt", "op": "remove", "value": "x@y.org"}}) == {"changed": True, "count": 0}
    with pytest.raises(SystemExit):
        lf.main({"args": {"file": "a.txt", "op": "explode"}})


def test_parse_dates():
    p = lib("parse_dates")
    text = "Meeting 2026-03-12, deadline 15.04.2026, trip 3 May 2026 and June 7th, 2026; bad 31.02.2026"
    out = p.main({"input": text, "args": {}})
    assert out["dates"] == ["2026-03-12", "2026-04-15", "2026-05-03", "2026-06-07"]
    assert p.main({"input": text, "args": {"after": "2026-05-01T00:00"}})["first"] == "2026-05-03"


def test_scripts_run_as_programs():
    # The sandbox runs them as `python3 -I -S main.py` with JSON on stdin.
    for name, req in [("pick", {"item": {"a": 1}, "args": {"fields": {"b": "item.a"}}}),
                      ("match_text", {"input": "hello", "args": {"any": ["hell"]}})]:
        p = subprocess.run([sys.executable, "-I", "-S", str(LIB / f"{name}.py")], input=json.dumps(req).encode(),
                           capture_output=True, timeout=30)
        assert p.returncode == 0, p.stderr
        json.loads(p.stdout)
