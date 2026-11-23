"""/protect: in protected chats and topics only what the owner tags is for Ampi."""

import asyncio

import pytest
from test_bot import CHAT, OWNER, msg, run, setup  # noqa: F401 (fixture)
from test_groups import GROUP, feed, gmsg, gsetup, settle  # noqa: F401 (fixture)

from assistant_bridge import bot as bot_mod
from assistant_bridge.store import Store

BOT_ID = 777


@pytest.fixture
def bot4(setup):  # noqa: F811
    bot, api, runner, store = setup()
    bot.username, bot.bot_id = "test_bot", BOT_ID
    return bot, api, runner, store


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.05)


def run(coro):  # noqa: F811  (a protected chat waits a moment for what belongs together)
    async def go():
        await coro
    asyncio.run(go())


def send(bot, *msgs):
    async def go():
        for m in msgs:
            await bot.handle_message(m)
        await settle(bot)
    asyncio.run(go())


def tag(text, **extra):
    at = len(text[:text.index("@")].encode("utf-16-le")) // 2
    word = text[text.index("@"):].split()[0]
    return msg(text, entities=[{"type": "mention", "offset": at, "length": len(word)}], **extra)


def test_a_protected_topic_stays_silent(bot4):
    bot, api, runner, store = bot4
    send(bot, (msg("/protect", thread=7)))
    assert "this topic" in api.sent[-1]["text"] and "@test_bot" in api.sent[-1]["text"]
    sent = len(api.sent)
    send(bot, (msg("just a note", thread=7)))
    assert not runner.calls and len(api.sent) == sent
    assert store.get_conversation("personal", CHAT, 7) is None  # nothing of it is kept
    send(bot, (msg("elsewhere")))
    send(bot, (msg("another topic", thread=8)))
    assert len(runner.calls) == 2
    send(bot, (tag("\U0001f600 hey @Test_Bot what about this?", thread=7)))
    assert len(runner.calls) == 3 and "what about this" in runner.calls[-1]["prompt"]


def test_files_in_a_protected_chat_are_not_downloaded(bot4):
    bot, api, runner, _ = bot4
    send(bot, (msg("/protect")))
    send(bot, (msg(document={"file_id": "x", "file_name": "a.pdf"})))
    assert not runner.calls and not (bot.profile.workspace / "inbox").exists()
    send(bot, (msg(document={"file_id": "x", "file_name": "a.pdf"}, caption="@test_bot read this",
                               caption_entities=[{"type": "mention", "offset": 0, "length": 9}])))
    assert len(runner.calls) == 1 and "a.pdf" in runner.calls[0]["prompt"]


@pytest.mark.parametrize("m,for_ampi", [
    (msg("x", reply_to_message={"message_id": 5, "from": {"id": BOT_ID}, "text": "hi"}), True),
    (msg("x", reply_to_message={"message_id": 5, "from": {"id": OWNER}, "text": "hi"}), False),
    (msg("x", reply_to_message={"message_id": 5, "from": {"id": BOT_ID}, "forum_topic_created": {"name": "t"}}), False),
    (msg("hey you", entities=[{"type": "text_mention", "offset": 0, "length": 3, "user": {"id": BOT_ID}}]), True),
    (msg("hey you", entities=[{"type": "text_mention", "offset": 0, "length": 3, "user": {"id": 5}}]), False),
    (tag("@other_bot hi"), False),
    (tag("@test_bot_2 hi"), False),
    (msg("test_bot, hi"), False),
    (tag("@test_bot hi", forward_origin={"type": "hidden_user", "sender_user_name": "X"}), False),
    (msg("x", entities=[{"type": "mention", "offset": 900, "length": 9}]), False),
    (msg("x", entities=[{"type": "mention"}]), False),
])
def test_what_counts_as_a_tag(bot4, m, for_ampi):
    bot, _, runner, _ = bot4
    send(bot, (msg("/protect")))
    send(bot, (m))
    assert bool(runner.calls) is for_ampi


def test_the_whole_chat_and_ending_it(bot4):
    bot, api, runner, _ = bot4
    send(bot, (msg("/protect chat", thread=7)))
    for m in (msg("a"), msg("b", thread=7), msg("c", thread=8)):
        send(bot, (m))
    assert not runner.calls
    send(bot, (msg("/protect off", thread=7)))
    assert "whole chat is" in api.sent[-1]["text"]
    send(bot, (msg("/status", thread=7)))
    assert "Protected: yes" in api.sent[-1]["text"]
    send(bot, (msg("/protect chat off", thread=8)))
    assert "No longer" in api.sent[-1]["text"]
    send(bot, (msg("b", thread=7)))
    assert len(runner.calls) == 1


def test_odd_words_change_nothing(bot4):
    bot, api, runner, _ = bot4
    for text in ("/protect me", "/protect on off"):
        send(bot, (msg(text)))
        assert "/protect chat" in api.sent[-1]["text"]
    send(bot, (msg("hello")))
    assert runner.calls


def test_nobody_else_can_change_it(bot4):
    bot, _, runner, store = bot4
    send(bot, (msg("/protect", user=999)))
    send(bot, (msg("/protect", forward_origin={"type": "hidden_user", "sender_user_name": "X"})))
    assert not bot._protected(CHAT, 0)


def test_a_group_batch_with_a_tag(gsetup):  # noqa: F811
    bot, api, runner, _ = gsetup()
    bot.bot_id = BOT_ID
    feed(bot, gmsg("/protect"))
    feed(bot, gmsg("a link"), gmsg("another"))
    assert not runner.calls
    fwd = gmsg("a forwarded post", forward_origin={"type": "hidden_user", "sender_user_name": "X"})
    at = [{"type": "mention", "offset": 0, "length": 9}]
    feed(bot, gmsg("private words"), fwd, gmsg("@test_bot what do you think?", entities=at), gmsg("more private words",
         document={"file_id": "p", "file_name": "passport.pdf"}))
    (call,) = runner.calls
    assert "a forwarded post" in call["prompt"] and "what do you think" in call["prompt"]
    assert "private words" not in call["prompt"] and "passport" not in call["prompt"]
    assert not (bot.profile.workspace / "inbox").exists()
    assert call["origin"] == "group:clipboard" and "Read(./inbox/mail/**)" in call["extra_denied"]


@pytest.mark.parametrize("first", ["/protect", "x"])
def test_what_counts_is_when_it_came(gsetup, first):  # noqa: F811
    bot, api, runner, _ = gsetup(batch=0.3)
    if first == "/protect":  # said under protection, which ends before it is handled
        feed(bot, gmsg("/protect"), gmsg("SECRET"), gmsg("/protect off"))
        assert not runner.calls
    else:  # wanted, and protection starts before it is handled
        feed(bot, gmsg("please do this"), gmsg("/protect"))
        assert len(runner.calls) == 1


def test_an_album_and_a_forward_with_words_belong_together(bot4):
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))
    photo = {"photo": [{"file_id": "p", "file_unique_id": "u"}], "media_group_id": "g1"}
    send(bot, msg(caption="@test_bot compare", caption_entities=[{"type": "mention", "offset": 0, "length": 9}], **photo),
         msg(**photo), msg(**photo), msg(photo=photo["photo"], media_group_id="g2"))
    assert len(runner.calls) == 1 and runner.calls[0]["prompt"].count("inbox/") == 3
    send(bot, msg("news", forward_origin={"type": "hidden_user", "sender_user_name": "X"}), tag("@test_bot what is this?"))
    assert len(runner.calls) == 2 and "news" in runner.calls[1]["prompt"] and runner.calls[1]["origin"] == "forwarded:private"


def test_a_tag_on_a_reply_fetches_the_file_it_answers(bot4):
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))
    old = {"message_id": 5, "from": {"id": OWNER}, "document": {"file_id": "d", "file_name": "plan.pdf"}}
    send(bot, tag("@test_bot read this", reply_to_message=old))
    assert "plan.pdf" in runner.calls[-1]["prompt"]
    send(bot, msg("/protect off"))
    send(bot, msg("and this", reply_to_message=old))
    assert "plan.pdf" not in runner.calls[-1]["prompt"]  # elsewhere it was fetched when it came


def test_an_answer_to_a_forward_is_not_only_the_owners_words(bot4):
    bot, api, runner, _ = bot4
    old = {"message_id": 5, "from": {"id": OWNER}, "text": "EVIL text", "forward_origin": {"type": "hidden_user", "sender_user_name": "X"}}
    send(bot, msg("sum up", reply_to_message=old))
    call = runner.calls[-1]
    assert call["origin"] == "forwarded:private" and call["tag"] is None and "Read(./inbox/mail/**)" in call["extra_denied"]


def test_the_topic_s_first_message_is_no_reply(bot4):
    bot, api, runner, _ = bot4
    send(bot, msg("/protect chat"))
    send(bot, msg("note", thread=7, reply_to_message={"message_id": 7, "from": {"id": BOT_ID}, "text": "New conversation"}))
    assert not runner.calls
    send(bot, msg("answer", thread=7, reply_to_message={"message_id": 9, "from": {"id": BOT_ID}, "text": "hi"}))
    assert len(runner.calls) == 1


def test_it_says_what_it_covers(bot4):
    bot, api, runner, _ = bot4
    send(bot, msg("/protect", thread=7))
    send(bot, msg("/protect"))
    assert "not its topics" in api.sent[-1]["text"]
    send(bot, msg("/protect chat"))
    send(bot, msg("/protect chat off"))
    assert "this chat itself" in api.sent[-1]["text"] and "1 topic" in api.sent[-1]["text"]
    send(bot, msg("hello", thread=8))
    assert "protected by the owner" in runner.calls[-1]["append_system_prompt"]


def test_a_group_that_becomes_a_supergroup_stays_protected(gsetup):  # noqa: F811
    bot, _, runner, store = gsetup()
    feed(bot, gmsg("/protect chat"))
    feed(bot, gmsg(migrate_to_chat_id=-100777))
    assert bot._protected(-100777, 5) and not bot._protected(GROUP, 5)


def test_files_from_emails_stay_in_the_private_chat(gsetup):  # noqa: F811
    from test_bot import FakeRunner

    bot, api, runner, _ = gsetup(runner=FakeRunner(reply="Here\nSEND_FILE: inbox/mail/x/a.pdf\nSEND_FILE: inbox/b.txt"))
    for f in ("inbox/mail/x/a.pdf", "inbox/b.txt"):
        (bot.profile.workspace / f).parent.mkdir(parents=True, exist_ok=True)
        (bot.profile.workspace / f).write_text("x")
    feed(bot, gmsg("hi"))
    assert [p.name for p, _, _ in api.files_sent] == ["b.txt"]
    feed(bot, msg("hi"))
    assert [p.name for p, _, _ in api.files_sent] == ["b.txt", "a.pdf", "b.txt"]


def test_settings_by_prefix():
    s = Store(":memory:")
    s.set_setting("p", "protected:1:0", "on")
    s.set_setting("p", "protected:12:0", "on")
    s.set_setting("q", "protected:1:5", "on")
    assert s.settings("p", "protected:1:") == {"protected:1:0": "on"}


def test_the_private_chat_is_not_the_group(gsetup):  # noqa: F811
    bot, _, runner, _ = gsetup()
    feed(bot, gmsg("/protect"))
    feed(bot, msg("hello"))
    assert len(runner.calls) == 1 and GROUP != CHAT


def test_a_reply_in_a_group_without_topics_is_a_tag(gsetup):  # noqa: F811
    bot, _, runner, _ = gsetup()
    bot.bot_id = BOT_ID
    feed(bot, gmsg("/protect"))
    # There a reply carries the id of the message its thread began with, which is Ampi's own.
    feed(bot, gmsg("thanks, and?", message_thread_id=50, reply_to_message={"message_id": 50, "from": {"id": BOT_ID}, "text": "Saved."}))
    assert len(runner.calls) == 1


def test_cancel_drops_what_waits(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.3)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))
    send(bot, tag("@test_bot delete everything"), msg("/reset"), msg("/cancel"))
    assert not runner.calls and "Dropped 1 message" in api.sent[-1]["text"] and "/cancel first" in api.sent[-2]["text"]
    send(bot, msg("/cancel"))
    assert "Nothing is running" in api.sent[-1]["text"]


def test_cancel_drops_what_waits_also_when_a_watcher_runs(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.3)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))

    async def go():
        watcher = asyncio.ensure_future(asyncio.sleep(60))
        bot._running[(CHAT, -3)] = watcher
        await bot.handle_message(tag("@test_bot delete everything"))
        await bot.handle_message(msg("/cancel"))
        del bot._running[(CHAT, -3)]
        await settle(bot)
        await asyncio.sleep(0.5)
        assert watcher.cancelled()
    asyncio.run(go())
    assert not runner.calls and "Stopped 1 watcher" in api.sent[-1]["text"] and "Dropped 1 message" in api.sent[-1]["text"]


def test_other_words_make_nothing_wait(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.3)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))

    async def go():
        await bot.handle_message(tag("@test_bot hi"))
        for _ in range(4):
            await asyncio.sleep(0.1)
            await bot.handle_message(msg("a note"))
        assert runner.calls  # 0.3 s after the tag, not after the last note
        assert all("a note" not in c["prompt"] for c in runner.calls)
        await settle(bot)
    asyncio.run(go())


def test_messages_stay_in_order(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.2)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))
    send(bot, tag("@test_bot first"), msg("/protect off"), msg("second"))
    (call,) = runner.calls
    assert call["prompt"].index("first") < call["prompt"].index("second")


def test_a_file_ampi_sent_is_not_fetched_again(bot4):
    bot, api, runner, _ = bot4
    bot.own_bot_ids = {BOT_ID}
    send(bot, msg("/protect"))
    send(bot, tag("@test_bot and this?", reply_to_message={"message_id": 9, "from": {"id": BOT_ID},
                                                          "document": {"file_id": "d", "file_name": "passport.pdf"}}))
    assert "passport" not in runner.calls[-1]["prompt"] and not (bot.profile.workspace / "inbox").exists()


def test_the_file_counts_as_when_the_message_came(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 0.2)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))
    old = {"message_id": 5, "from": {"id": OWNER}, "document": {"file_id": "d", "file_name": "plan.pdf"}}
    send(bot, tag("@test_bot read this", reply_to_message=old), msg("/protect off"))
    assert "plan.pdf" in runner.calls[-1]["prompt"]


def test_a_restart_says_what_was_not_handled(bot4, monkeypatch):
    monkeypatch.setattr(bot_mod, "PROTECT_SECONDS", 5)
    bot, api, runner, _ = bot4
    send(bot, msg("/protect"))

    async def go():
        await bot.handle_message(tag("@test_bot hi"))
        await bot.shutdown()
    asyncio.run(go())
    assert "restarted" in api.sent[-1]["text"] and not runner.calls
