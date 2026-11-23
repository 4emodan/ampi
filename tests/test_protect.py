"""/protect: in protected chats and topics only what the owner tags is for Ampi."""

import pytest
from test_bot import CHAT, OWNER, msg, run, setup  # noqa: F401 (fixture)
from test_groups import GROUP, feed, gmsg, gsetup  # noqa: F401 (fixture)

BOT_ID = 777


@pytest.fixture
def bot4(setup):  # noqa: F811
    bot, api, runner, store = setup()
    bot.username, bot.bot_id = "test_bot", BOT_ID
    return bot, api, runner, store


def tag(text, **extra):
    at = len(text[:text.index("@")].encode("utf-16-le")) // 2
    word = text[text.index("@"):].split()[0]
    return msg(text, entities=[{"type": "mention", "offset": at, "length": len(word)}], **extra)


def test_a_protected_topic_stays_silent(bot4):
    bot, api, runner, store = bot4
    run(bot.handle_message(msg("/protect", thread=7)))
    assert "this topic" in api.sent[-1]["text"] and "@test_bot" in api.sent[-1]["text"]
    sent = len(api.sent)
    run(bot.handle_message(msg("just a note", thread=7)))
    assert not runner.calls and len(api.sent) == sent
    assert store.get_conversation("personal", CHAT, 7) is None  # nothing of it is kept
    run(bot.handle_message(msg("elsewhere")))
    run(bot.handle_message(msg("another topic", thread=8)))
    assert len(runner.calls) == 2
    run(bot.handle_message(tag("\U0001f600 hey @Test_Bot what about this?", thread=7)))
    assert len(runner.calls) == 3 and "what about this" in runner.calls[-1]["prompt"]


def test_files_in_a_protected_chat_are_not_downloaded(bot4):
    bot, api, runner, _ = bot4
    run(bot.handle_message(msg("/protect")))
    run(bot.handle_message(msg(document={"file_id": "x", "file_name": "a.pdf"})))
    assert not runner.calls and not (bot.profile.workspace / "inbox").exists()
    run(bot.handle_message(msg(document={"file_id": "x", "file_name": "a.pdf"}, caption="@test_bot read this",
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
    run(bot.handle_message(msg("/protect")))
    run(bot.handle_message(m))
    assert bool(runner.calls) is for_ampi


def test_the_whole_chat_and_ending_it(bot4):
    bot, api, runner, _ = bot4
    run(bot.handle_message(msg("/protect chat", thread=7)))
    for m in (msg("a"), msg("b", thread=7), msg("c", thread=8)):
        run(bot.handle_message(m))
    assert not runner.calls
    run(bot.handle_message(msg("/protect off", thread=7)))
    assert "whole chat is" in api.sent[-1]["text"]
    run(bot.handle_message(msg("/status", thread=7)))
    assert "Protected: yes" in api.sent[-1]["text"]
    run(bot.handle_message(msg("/protect chat off", thread=8)))
    assert "No longer" in api.sent[-1]["text"]
    run(bot.handle_message(msg("b", thread=7)))
    assert len(runner.calls) == 1


def test_odd_words_change_nothing(bot4):
    bot, api, runner, _ = bot4
    for text in ("/protect me", "/protect on off"):
        run(bot.handle_message(msg(text)))
        assert "/protect chat" in api.sent[-1]["text"]
    run(bot.handle_message(msg("hello")))
    assert runner.calls


def test_nobody_else_can_change_it(bot4):
    bot, _, runner, store = bot4
    run(bot.handle_message(msg("/protect", user=999)))
    run(bot.handle_message(msg("/protect", forward_origin={"type": "hidden_user", "sender_user_name": "X"})))
    assert not bot._protected(CHAT, 0)


def test_a_group_batch_with_a_tag_is_handled_together(gsetup):  # noqa: F811
    bot, api, runner, _ = gsetup()
    bot.bot_id = BOT_ID
    feed(bot, gmsg("/protect"))
    feed(bot, gmsg("a link"), gmsg("another"))
    assert not runner.calls
    fwd = gmsg("a forwarded post", forward_origin={"type": "hidden_user", "sender_user_name": "X"})
    feed(bot, fwd, gmsg("@test_bot what do you think?", entities=[{"type": "mention", "offset": 0, "length": 9}]))
    (call,) = runner.calls
    assert "a forwarded post" in call["prompt"] and "what do you think" in call["prompt"]
    assert call["origin"] == "group:clipboard"


def test_the_private_chat_is_not_the_group(gsetup):  # noqa: F811
    bot, _, runner, _ = gsetup()
    feed(bot, gmsg("/protect"))
    feed(bot, msg("hello"))
    assert len(runner.calls) == 1 and GROUP != CHAT
