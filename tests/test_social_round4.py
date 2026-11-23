"""Fourth review of the social session: Telegram as a way back for social text, GraphQL errors, stop signals, the DM
watcher's queue, session-file races. Fakes only."""

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from test_bot import CHAT, msg
from test_social_fixes import SchemaClaude, dm_prompts, engine_for, run_once, thread
from test_social_session import (  # noqa: F401 (fixtures)
    BRIDGE, IG_COOKIES, MEDIA_CODE, TH_CODE, ask, approve, known_docs, run, sbot, session_file, web,
)
from test_tg_reader import ANNA, CLIP, FakeClient, dialog, message

from ampi_tools import meta_session as msess
from ampi_tools.meta_session import IG, Budget, SessionAuth, store_session
from ampi_tools.ops_telegram import TelegramStore
from ampi_tools.todoist import ToolError
from assistant_bridge.telegram import TelegramError
from assistant_bridge.watch.pipeline import _defang
from tg_reader.reader import Reader, UserError
from tg_reader.writer import Writer

SECRET = "SECRET-SOCIAL-TEXT of some length"
AMPI = NS(id=777111, first_name="Ampi", last_name=None, username="my_ampi_bot", bot=True)
LINK = f"https://www.instagram.com/p/{MEDIA_CODE}/"
TLINK = f"https://www.threads.com/@carol/post/{TH_CODE}"


# --- A: reading Telegram ---------------------------------------------------------------------------------------


def reader(deny=True, forwards=()):
    dialogs = [dialog(CLIP, "Clipboard", is_group=True, is_channel=True), dialog(ANNA, "Anna K", is_user=True),
               dialog(AMPI, "Ampi", is_user=True)]
    msgs = {AMPI.id: [message(60, SECRET, sender=AMPI, chat=AMPI, chat_id=AMPI.id)],
            ANNA.id: [message(50, "the flat is ready", chat=ANNA, chat_id=ANNA.id)],
            CLIP.id: [message(70, SECRET, chat_id=-100, forward=f) for f in forwards]}
    r = Reader(FakeClient(dialogs, msgs))
    if deny and hasattr(r, "set_own_bots"):
        r.set_own_bots([AMPI.id], True)
    return r


@pytest.mark.parametrize("req", [
    {"cmd": "history", "chat": "Ampi"}, {"cmd": "history", "chat": "@my_ampi_bot"},
    {"cmd": "history", "chat": str(AMPI.id)}, {"cmd": "history", "chat": "Ampi", "search": "SECRET"},
    {"cmd": "search", "query": "SECRET"}, {"cmd": "since", "chat": "Ampi", "after_id": 0},
    {"cmd": "since", "chat": AMPI.id, "after_id": 0},
])
def test_no_read_path_shows_the_bridges_own_bot_chat(req):
    assert SECRET not in json.dumps(run(reader().handle(req)))


def test_the_own_bot_chat_is_not_listed_and_others_still_work():
    r = reader()
    assert "Ampi" not in run(r.chats())
    assert "flat is ready" in run(r.handle({"cmd": "history", "chat": "Anna K"}))["text"]
    assert "flat is ready" in run(r.handle({"cmd": "search", "query": "flat"}))["text"]


@pytest.mark.parametrize("fwd", [NS(sender=AMPI, chat=None, sender_id=AMPI.id),
                                 NS(sender=None, chat=None, sender_id=AMPI.id),
                                 NS(sender=None, chat=None, from_id=NS(user_id=AMPI.id)),
                                 NS(sender=None, chat=None, saved_from_peer=NS(user_id=AMPI.id))])
def test_the_bots_messages_forwarded_elsewhere_lose_their_text(fwd):
    out = run(reader(forwards=[fwd]).handle({"cmd": "history", "chat": "Clipboard"}))
    assert out["ok"] and SECRET not in out["text"]


def test_a_forward_from_a_person_is_still_readable():
    out = run(reader(forwards=[NS(sender=ANNA, chat=None, sender_id=ANNA.id)]).handle(
        {"cmd": "history", "chat": "Clipboard"}))
    assert SECRET in out["text"]


def test_reads_fail_closed_while_the_own_bot_is_unknown(tmp_path):
    r = reader(deny=False)
    r.guard(tmp_path / "bots.json", required=True)
    assert "isn't known yet" in run(r.handle({"cmd": "chats"}))["error"]
    r.set_own_bots([], True)  # a social session, but no bot id
    assert not run(r.handle({"cmd": "history", "chat": "Anna K"}))["ok"]
    r.set_own_bots([AMPI.id], True)
    assert run(r.handle({"cmd": "history", "chat": "Anna K"}))["ok"]
    again = reader(deny=False)  # a restart
    again.guard(tmp_path / "bots.json", required=True)
    assert run(again.handle({"cmd": "history", "chat": "Anna K"}))["ok"]
    assert not run(again.handle({"cmd": "history", "chat": "Ampi"}))["ok"]
    again.set_own_bots([], False)  # nothing once denied is ever opened again
    assert not run(again.handle({"cmd": "history", "chat": "Ampi"}))["ok"]
    (tmp_path / "bots.json").write_text("garbage")
    broken = reader(deny=False)
    broken.guard(tmp_path / "bots.json", required=True)
    assert not run(broken.handle({"cmd": "chats"}))["ok"]


def test_without_ampi_tools_reading_works_as_before(tmp_path):
    r = reader(deny=False)
    r.guard(tmp_path / "bots.json", required=False)
    assert run(r.handle({"cmd": "chats"}))["ok"]


def test_writers_message_lookups_do_not_return_the_bots_forwards():
    r = reader(forwards=[NS(sender=AMPI, chat=None, sender_id=AMPI.id)])

    async def get_messages(entity, ids=None):
        return [m for m in r.client.messages[CLIP.id] if m.id in ids]

    r.client.__dict__["get_messages"] = get_messages
    w = Writer(r, 1)
    d = next(d for d in r.client.dialogs if d.name == "Clipboard")
    with pytest.raises(UserError):
        run(w.messages(d.id, d, [70]))


def test_the_write_socket_takes_the_deny_list_and_reports_it():
    r = reader(deny=False)
    w = Writer(r, 1)
    assert run(w.handle({"cmd": "own_bots", "bots": [AMPI.id], "social": True}))["active"] is True
    assert not run(r.handle({"cmd": "history", "chat": "Ampi"}))["ok"]
    assert run(w.handle({"cmd": "own_bots_status"}))["active"] is True


def test_the_bridge_tells_which_bots_are_its_own(web, tmp_path):
    w = web()
    store = TelegramStore(tmp_path / "t.sqlite3")
    w.daemon.services.telegram_side = NS(store=store)
    run(w.daemon.handle({"cmd": "telegram.protect", "ids": [5, 6], "bots": [5]}, BRIDGE))
    assert store.own_bots() == [5]
    run(w.daemon.handle({"cmd": "telegram.protect", "ids": [8], "bots": [8]}, BRIDGE))
    assert store.own_bots() == [5, 8]  # an old bot's chat still holds its messages


def test_ampi_tools_passes_the_bots_and_the_session_state_on(web):
    from ampi_tools.server import tell_tg_reader
    w = web()
    told = []

    async def tell(social):
        told.append(social)

    w.daemon.services.telegram_side = NS(client=NS(tell_own_bots=tell))
    run(tell_tg_reader(w.daemon.services))
    w.ig.auth.forget()
    w.th.auth.forget()
    run(tell_tg_reader(w.daemon.services))
    assert told == [True, False]


def test_the_relay_reports_the_bridges_bots(sbot):
    from assistant_bridge.approvals import ApprovalRelay
    sent = []

    async def ask_fn(socket, payload, timeout=60):
        sent.append(payload)
        return {"ok": True}

    relay = ApprovalRelay(sbot[0], "x", ask_fn, protected=lambda: ([1], True))
    relay.own_bots = [777111]
    run(relay.send_protected())
    assert sent[0]["bots"] == [777111]


def test_a_session_is_not_saved_before_the_deny_is_active(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(msess.getpass, "getpass", lambda p: asked.append(p) or "x" * 12)

    def refuse(home):
        raise ToolError("tg-reader doesn't keep Ampi's own chat out yet")

    with pytest.raises(SystemExit, match="Not saved"):
        msess.session_command(tmp_path, IG, guard=refuse)
    assert asked == [] and list(tmp_path.iterdir()) == []


# --- B: GraphQL and REST errors ---------------------------------------------------------------------------------


@pytest.mark.parametrize("err,want", [
    ({"message": "login_required"}, "challenge"), ({"summary": "Please log in"}, "challenge"),
    ({"description": "checkpoint_required"}, "challenge"), ({"code": 1357001}, "challenge"),
    ({"message": "x", "extensions": {"code": "challenge_required"}}, "challenge"),
    ({"code": 401}, "challenge"), ({"message": "Please wait a few minutes before you try again."}, "slow_down"),
    ({"code": 429}, "slow_down"), ({"message": "useragent mismatch"}, "user_agent"),
    ({"code": 1357004}, "stale_tokens"), ({"message": "some field failed"}, None),
])
def test_every_graphql_error_entry_is_checked(err, want):
    assert msess._problem(200, {"data": None, "errors": [{"message": "ok"}, err]}, "", "graphql") == want


def test_error_summary_tokens():
    assert msess._problem(200, {"errorSummary": "login_required"}, "") == "challenge"


def like_reply(w, body):
    real = w.fake.threads
    doc = msess.TH_DOCS["like"].doc_id
    w.fake.threads = lambda m, p, f: (httpx.Response(200, json=body) if f.get("doc_id") == doc else real(m, p, f))


@pytest.mark.parametrize("body", [
    {"data": {"like": {"status": "ok"}}, "errors": [{"message": "denied"}]}, {"data": {"like": None}}, {"data": {}},
    {"data": None}, {"data": {"like": {}}}, {"data": {"like": {"status": "fail"}}}, {}, {"data": []},
])
def test_a_write_without_a_clean_result_is_never_reported_done(web, body):
    w = web()
    ask(w, ("thweb.like", {"target": TLINK}))
    like_reply(w, body)
    out = approve(w)
    assert out["status"] == "failed" and "Liked" not in out["text"]


@pytest.mark.parametrize("data", [{"feedData": None}, {"feedData": {}}, {"feedData": []}, {}])
def test_an_empty_expected_result_is_a_failure(web, data):
    w = web()
    w.fake.override[("www.threads.com", "POST", "/graphql/query")] = httpx.Response(200, json={"data": data})
    assert not ask(w, ("thweb.feed", {}))["ok"]


def test_rest_200_with_an_error_fails(web):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    w.fake.override[("POST", f"/api/v1/web/likes/{msess.shortcode_to_pk(MEDIA_CODE)}/like/")] = httpx.Response(
        200, json={"error": 1234})
    assert approve(w)["status"] == "failed"


# --- C: other stop signals --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 302, 307])
def test_undecodable_stop_replies_still_pause(web, status):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = lambda r: httpx.Response(
        status, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip"))
    ask(w, ("igweb.stories", {}))
    assert w.ig.auth.state() == "paused"


def test_a_huge_reply_is_not_read_to_the_end(web, monkeypatch):
    w = web()
    monkeypatch.setattr(msess, "MAX_REPLY", 1000)
    read = []

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10_000):
                read.append(i)
                yield b"x" * 500

    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = lambda r: httpx.Response(200, stream=Endless())
    assert "too big" in ask(w, ("igweb.stories", {}))["error"]
    assert len(read) < 20


@pytest.mark.parametrize("data", [
    {"error_type": "login_required"}, {"logout_reason": 3}, {"user_has_logged_out": True},
    {"message": "consent_required"}, {"message": "checkpoint_challenge_required"}, {"error_type": "sentry_block"},
])
def test_more_stop_fields(data):
    assert msess._problem(200, data, "") == "challenge"


def test_slow_down_error_type():
    assert msess._problem(400, {"error_type": "rate_limit_error"}, "") == "slow_down"


@pytest.mark.parametrize("status,paused", [(200, True), (404, False), (500, False), (502, False)])
def test_non_json_from_a_json_endpoint_pauses_unless_the_server_failed(web, status, paused):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(status, text="<html>hello</html>")
    assert not ask(w, ("igweb.stories", {}))["ok"]
    assert (w.ig.auth.state() == "paused") is paused


def test_a_list_where_an_object_is_expected_is_an_error(web):
    w = web()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = httpx.Response(200, json=[1])
    resp = ask(w, ("igweb.stories", {}))
    assert not resp["ok"] and "unexpected" in resp["error"]


def test_a_login_page_with_tokens_is_still_a_login_page(web):
    from test_social_session import PAGE
    w = web()
    w.fake.th_page = PAGE + '<form id="loginForm"></form>'
    ask(w, ("thweb.feed", {}))
    assert w.th.auth.state() == "paused" and w.th.auth.load()["paused_reason"] == "login_required"


# --- D: forwards ---------------------------------------------------------------------------------------------------


def social_bot(sbot, text=SECRET):
    bot, api, runner, store = sbot
    ids = iter(range(100, 999))

    async def send(chat_id, t, thread_id=None, parse_mode=None):
        api.sent.append({"chat_id": chat_id, "text": t})
        return {"message_id": next(ids), "text": text if text in t else t}

    api.send_message = send
    runner.reply = text
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("feed")))
    run(bot.handle_message(msg("/reset")))
    runner.reply = "ok"
    return bot, runner


OWN = {"type": "user", "sender_user": {"id": 777111, "is_bot": True, "first_name": "Ampi"}}
HIDDEN = {"type": "hidden_user", "sender_user_name": "Ampi"}


@pytest.mark.parametrize("extra", [
    {"text": SECRET, "forward_origin": OWN}, {"caption": SECRET, "forward_origin": OWN},
    {"text": SECRET, "forward_origin": HIDDEN}, {"text": SECRET, "forward_origin": {}},
    {"text": SECRET, "forward_origin": {"type": "new_kind"}},
    {"text": "look", "reply_to_message": {"message_id": 5, "text": SECRET, "forward_origin": OWN}},
    {"text": "look", "reply_to_message": {"message_id": 5, "caption": SECRET, "forward_origin": HIDDEN}},
])
def test_social_text_forwarded_into_a_normal_topic_is_dropped(sbot, extra):
    bot, runner = social_bot(sbot)
    bot.own_bot_ids = {777111}
    n = len(runner.calls)
    run(bot.handle_message(msg(thread=9, **extra)))
    for c in runner.calls[n:]:
        assert SECRET not in c["prompt"]


def test_an_edited_approval_is_recognised_too(sbot):
    bot, runner = social_bot(sbot)
    bot.store.add_social_message(CHAT, 300, "the approval with its answer added")
    run(bot.handle_message(msg("the approval with its answer added", thread=9, forward_origin=HIDDEN)))
    assert "its answer added" not in runner.calls[-1]["prompt"]


def test_other_forwards_still_work(sbot):
    bot, runner = social_bot(sbot)
    run(bot.handle_message(msg("news from a friend", thread=9, forward_origin=HIDDEN)))
    assert "news from a friend" in runner.calls[-1]["prompt"]


def test_mixed_batches_drop_only_the_social_forward(tmp_path):
    from test_groups import GROUP, gmsg
    from test_bot import FakeAPI, FakeRunner
    from assistant_bridge.bot import ProfileBot
    from assistant_bridge.config import Group, Profile
    from assistant_bridge.store import Store
    profile = Profile(name="personal", bot_token_env="TG", bot_token="777111:x", owner_ids=frozenset({CHAT}),
                      workspace=tmp_path / "w", model="sonnet", allowed_tools=("Read",), extra_args=(),
                      groups=(Group(chat_id=GROUP, name="clip", batch_seconds=0.05, fresh_session=True),))
    profile.workspace.mkdir()
    runner = FakeRunner(reply="ok")
    bot = ProfileBot(profile, FakeAPI(), Store(":memory:"), runner)

    async def go():
        await bot.handle_message(gmsg("normal one"))
        await bot.handle_message(gmsg(SECRET, forward_origin=OWN))
        while bot._tasks:
            await asyncio.gather(*list(bot._tasks), return_exceptions=True)

    run(go())
    assert "normal one" in runner.calls[0]["prompt"] and SECRET not in runner.calls[0]["prompt"]


# --- E: one bad batch doesn't block the rest -------------------------------------------------------------------------


def test_a_batch_the_ai_cannot_handle_is_counted_and_passed(tmp_path):
    class Poison(SchemaClaude):
        async def __call__(self, prompt, **kw):
            if "POISON" in prompt:
                self.calls.append((prompt, kw))
                return NS(ok=False, text="", error="the model refused")
            return await super().__call__(prompt, **kw)

    items = [("a", 10, "POISON")]
    claude = Poison([])
    e, w, bot, _ = engine_for(tmp_path, claude, lambda: [thread(*items)])
    assert run_once(e, w) == "failed"
    items.append(("b", 11, "newer message"))  # doesn't restart the count
    assert run_once(e, w) == "failed"
    n = len(claude.calls)
    assert run_once(e, w) == "ok"
    assert len(claude.calls) == n and "POISON" not in "".join(bot.sent) and "1 " in bot.sent[-1]
    assert run_once(e, w) == "ok" and "newer message" in dm_prompts(claude)[-1]


def test_malformed_items_do_not_stop_the_watcher(tmp_path):
    from ampi_tools import social_dm
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "fine"))])
    tools = e.tools

    async def odd(payload, timeout=300):
        out = await tools(payload)
        if payload.get("cmd") == "watch.run":
            out["threads"] = [{"thread": "9"}, None, {**out["threads"][0], "items": [{"id": 1}, *out["threads"][0]["items"]]}]
        return out

    e.tools = e.pipeline.svc.tools = odd
    assert run_once(e, w) == "ok"
    assert social_dm


def test_a_message_telegram_never_accepts_becomes_a_count(tmp_path):
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "x"))])
    send = bot._send_text

    async def picky(chat_id, thread_id, text, **kw):
        if SECRET in text or "in a summary" in text:
            raise TelegramError("sendMessage", "bad request", 400)
        await send(chat_id, thread_id, text, **kw)

    bot._send_text = picky
    results = [run_once(e, w) for _ in range(8)]
    assert "ok" in results and bot.sent


# --- F, G, H, I ----------------------------------------------------------------------------------------------------


def test_the_stale_count_is_per_session_and_cleared_by_other_replies(web):
    w = web()
    real = w.fake.threads
    stale = httpx.Response(200, json={"error": 1357004})
    replies = iter([stale, httpx.Response(200, json={"data": {"x": 1}}), stale])

    def threads(m, p, f):
        return next(replies, None) or real(m, p, f) if p == "/graphql/query" else real(m, p, f)

    w.fake.threads = threads
    ask(w, ("thweb.feed", {}))            # stale, then a (failed) non-stale reply
    assert ask(w, ("thweb.feed", {}))["ok"]  # stale once: refreshed, not paused
    assert w.th.auth.state() == "ok"
    w.th._stale.append({"old": 1})
    w.th.auth.resume()
    ask(w, ("thweb.feed", {}))
    assert w.th._stale == []


def test_no_gap_note_when_the_owner_wrote_in_the_window(tmp_path):
    t = thread(("a", 10, "hello"), has_older=True)
    t["items"].append({"item_id": "o", "user_id": 1001, "timestamp": 5, "item_type": "text", "text": "me"})
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [t])
    run_once(e, w)
    assert "earlier" not in bot.sent[-1]


def test_a_late_message_with_a_lower_time_is_reported(tmp_path):
    items = [("b", 20, "second")]
    claude = SchemaClaude([])
    e, w, _, space = engine_for(tmp_path, claude, lambda: [thread(*items)])
    run_once(e, w)
    items.append(("a", 15, "late one"))
    assert run_once(e, w) == "ok" and "late one" in dm_prompts(claude)[-1] and "second" not in dm_prompts(claude)[-1]
    items += [(f"n{i}", 100 + i, "m") for i in range(400)]
    while run_once(e, w) == "ok":
        pass
    assert len(space["cursors.json"]) < 12_000


@pytest.mark.parametrize("err,retry", [(TelegramError("sendMessage", "network error: boom", None), True),
                                       (TelegramError("sendMessage", "Too Many Requests", 429), True),
                                       (TelegramError("sendMessage", "Bad Gateway", 502), True),
                                       (TelegramError("sendMessage", f"can't parse {SECRET}", 400), False)])
def test_social_send_errors_keep_whether_a_retry_makes_sense(tmp_path, err, retry):
    e, w, bot, _ = engine_for(tmp_path, SchemaClaude([]), lambda: [thread(("a", 10, "x"))])

    async def broken(*a, **kw):
        raise err

    bot._send_text = broken
    run_once(e, w)
    row = e.db.runs("personal", w.name)[0]
    assert row["status"] == ("retrying" if retry else "failed") and SECRET not in json.dumps(row)


@pytest.mark.parametrize("old", [{"1": 10}, {"1": {"ts": 10, "ids": ["a"]}}])
def test_old_cursor_files_are_understood(tmp_path, old):
    claude = SchemaClaude([])
    e, w, _, space = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "reported"), ("b", 11, "new one"))])
    space["cursors.json"] = json.dumps(old).encode()
    assert run_once(e, w) == "ok"
    assert "new one" in dm_prompts(claude)[-1] and "reported" not in dm_prompts(claude)[-1]


# --- J, K ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["/reset", "/new"])
def test_reset_while_an_attachment_downloads_lets_no_social_quote_through(sbot, command):
    bot, api, runner, store = sbot
    api.topics = False
    started, go_on = asyncio.Event(), asyncio.Event()

    async def slow(file_id):
        started.set()
        await go_on.wait()
        return {"file_path": "x"}

    api.get_file = slow
    store.set_social("personal", CHAT, 0)
    store.add_social_message(CHAT, 55, SECRET)

    async def go():
        task = asyncio.create_task(bot.handle_message(msg("see", reply_to_message={"message_id": 55, "text": SECRET},
                                                          document={"file_id": "d", "file_name": "a.txt"})))
        await asyncio.wait_for(started.wait(), 5)
        await bot.handle_message(msg(command))
        go_on.set()
        await task

    run(go())
    for c in runner.calls:
        assert c["origin"] == "forwarded:social" or SECRET not in c["prompt"]


@pytest.mark.parametrize("rules", [("Read", "Bash(instagram *)"), ("Bash(instagram:*, x)",), ("Read",)])
def test_a_misspelt_rule_is_said_instead_of_a_run_without_tools(sbot, rules):
    bot, api, runner, store = sbot
    bot.profile = dataclasses.replace(bot.profile, allowed_tools=rules)
    run(bot.handle_message(msg("/social")))
    run(bot.handle_message(msg("feed")))
    assert runner.calls == [] and "Bash(instagram:*)" in api.sent[-1]["text"]


# --- L: a replaced session ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", [
    lambda: httpx.Response(200, json={"tray": []}, headers={"set-cookie": "csrftoken=OLDSESSION_csrf_1; Path=/"}),
    lambda: httpx.Response(302, headers={"location": "/accounts/login/"}),
    lambda: httpx.Response(429, json={}),
    lambda: httpx.Response(200, json={"tray": []}, headers={"set-cookie": 'sessionid=""; Max-Age=0'}),
])
def test_a_late_reply_of_the_old_session_leaves_the_new_one_alone(web, reply):
    w = web()
    path = w.ig.auth.path

    def replace_then(request):
        session_file(path, {**IG_COOKIES, "sessionid": "NEWSESSION_secret_9"}, "me_ig", saved_at=time.time() + 5,
                     sid="new")
        return reply()

    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = replace_then
    ask(w, ("igweb.stories", {}))
    d = json.loads(path.read_text())
    assert d["state"] == "ok" and not d.get("cooldown_until") and d["cookies"]["csrftoken"] == IG_COOKIES["csrftoken"]
    assert d["cookies"]["sessionid"] == "NEWSESSION_secret_9"


def test_a_request_of_a_replaced_session_is_not_sent(web):
    w = web()
    sent = []

    async def swap(_):
        session_file(w.ig.auth.path, {**IG_COOKIES, "sessionid": "NEWSESSION_secret_9"}, "me_ig",
                     saved_at=time.time() + 5, sid="new")

    w.ig.gap, w.ig._sleep, w.ig._last = 1000.0, swap, w.ig._clock()
    w.fake.override[("GET", "/api/v1/feed/reels_tray/")] = lambda r: sent.append(1) or httpx.Response(200, json={})
    assert not ask(w, ("igweb.stories", {}))["ok"]
    assert sent == []


def test_usage_counts_of_two_processes_add_up(tmp_path):
    a, b = Budget(tmp_path / "u.json"), Budget(tmp_path / "u.json")
    a.take("read", False, "x")
    b.take("read", False, "x")
    assert len(json.loads((tmp_path / "u.json").read_text())["read"]) == 2


def test_the_session_file_is_locked_across_processes(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "instagram-session.json"
    session_file(path, IG_COOKIES, "me_ig")
    code = ("import sys,time,pathlib\nfrom ampi_tools.meta_session import file_lock\n"
            "with file_lock(pathlib.Path(sys.argv[1])):\n print('locked', flush=True)\n time.sleep(1.5)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    t = time.monotonic()
    SessionAuth(IG, path).rotate("csrftoken", "ROTATED_value_1")
    assert time.monotonic() - t > 0.8
    p.wait()


def test_each_saved_session_has_its_own_identity(tmp_path):
    from test_social_session import FakeMeta
    c = httpx.AsyncClient(transport=httpx.MockTransport(FakeMeta()))
    one = run(store_session(tmp_path, IG, dict(IG_COOKIES), c))
    two = run(store_session(tmp_path, IG, dict(IG_COOKIES), c))
    assert one["sid"] and one["sid"] != two["sid"]


# --- M, N, O -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["evil․com", "evil。com", "evil．com", "evil｡com", "evil﹒com", "a.b", "x!.com", "é.com",
                                  "9.com", "1.2.3.4", "ev​il.c​om", "☃.net"])
def test_every_dot_inside_a_word_is_broken(text):
    out = _defang(text, True)
    import re
    assert not re.search(r"\S[.․。．｡﹒](?=\S)(?!\))", out.replace("(.)", "")), out


def test_the_dm_ai_call_loads_no_settings(tmp_path):
    claude = SchemaClaude([])
    e, w, _, _ = engine_for(tmp_path, claude, lambda: [thread(("a", 10, "x"))])
    run_once(e, w)
    assert claude.calls[0][1].get("setting_sources") == ""


@pytest.mark.parametrize("exc,maybe", [(httpx.ReadTimeout("x"), True), (httpx.WriteError("x"), True),
                                       (httpx.RemoteProtocolError("x"), True), (httpx.ConnectError("x"), False),
                                       (httpx.ConnectTimeout("x"), False)])
def test_a_write_without_an_answer_says_it_may_have_happened(web, exc, maybe):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    path = f"/api/v1/web/likes/{msess.shortcode_to_pk(MEDIA_CODE)}/like/"

    def fail(r):
        raise exc

    w.fake.override[("POST", path)] = fail
    out = approve(w)
    assert out["status"] == "failed" and ("may have" in out["text"].lower()) is maybe
    assert sum(c.path == path for c in w.fake.calls) == 1


def test_a_server_error_on_a_write_says_so_too(web):
    w = web()
    ask(w, ("igweb.like", {"target": LINK}))
    w.fake.override[("POST", f"/api/v1/web/likes/{msess.shortcode_to_pk(MEDIA_CODE)}/like/")] = httpx.Response(502)
    assert "may have" in approve(w)["text"].lower()


_ = Path
