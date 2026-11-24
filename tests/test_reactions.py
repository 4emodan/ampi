"""Emoji reactions: what Ampi may react to, the status of a task, and that the lines never show."""

import asyncio

import pytest
from test_bot import CHAT, FakeAPI, FakeRunner, msg, run, setup  # noqa: F401 (fixture)

from assistant_bridge import reactions
from assistant_bridge.telegram import TelegramError


class API(FakeAPI):
    def __init__(self, fail=False):
        super().__init__()
        self.set, self.fail = [], fail

    async def set_message_reaction(self, chat_id, message_id, emoji):
        if self.fail:
            raise TelegramError("setMessageReaction", "REACTION_INVALID", 400)
        self.set.append((message_id, emoji))


def m(i, text="do it", **kw):
    return {**msg(text, **kw), "message_id": i}


@pytest.mark.parametrize("text,out,found", [
    ("Done.\nREACT: 🔥", "Done.", [(None, "🔥")]), ("REACT 812: ✅", "", [(812, "👌")]), ("REACT 5: none", "", [(5, "")]),
    ("REACT: ❤️", "", [(None, "❤")]), ("REACT: 🦖", "", []), ("a REACT: 👍", "a REACT: 👍", []),
    ("```\nREACT: 👍\n```", "```\nREACT: 👍\n```", []), ("he wrote\nREACT: 👍\nso", "he wrote\nREACT: 👍\nso", []),
    ("a\n\nREACT 1: 👍\nREACT 2: 🔥\n", "a", [(1, "👍"), (2, "🔥")]), ("```\nREACT: 👍", "```\nREACT: 👍", []), ("no lines", "no lines", [])])
def test_parse(text, out, found):
    assert reactions.parse(text) == (out, found)


def test_everything_offered_is_allowed():
    assert set(reactions.ALIASES.values()) | {reactions.SEEN, reactions.DONE, reactions.FAILED} <= reactions.ALLOWED
    assert reactions.parse("\n".join("REACT: 👍" for _ in range(30)))[1] == [(None, "👍")] * 10


def test_status_of_a_task(setup):  # noqa: F811
    bot, api, runner, _ = setup(api=API())
    run(bot.handle_message(m(7)))
    assert api.set == [(7, "👀"), (7, "👌")] and "[message 7]" in runner.calls[0]["prompt"]
    assert runner.calls[0]["tag"][-1] == "do it"  # the owner's words without the id
    runner.ok = False
    run(bot.handle_message(m(8)))
    assert api.set[-1] == (8, "👎")


def test_own_choice_and_only_a_reaction(setup):  # noqa: F811
    bot, api, runner, _ = setup(api=API(), runner=FakeRunner("REACT: 🏆"))
    run(bot.handle_message(m(7)))
    assert api.set == [(7, "👀"), (7, "🏆")] and api.sent == []


def test_only_the_owners_messages_of_this_conversation(setup):  # noqa: F811
    bot, api, runner, store = setup(api=API(), runner=FakeRunner("ok\nREACT 5: 🔥\nREACT 6: 🔥\nREACT 99: 🔥"))
    store.add_owner_message("personal", CHAT, 0, 5)
    store.add_owner_message("personal", CHAT, 33, 6)
    run(bot.handle_message(m(7)))
    assert (5, "🔥") in api.set and not any(i in (6, 99) for i, _ in api.set)
    assert "REACT" not in api.sent[0]["text"]


def test_off_per_topic(setup):  # noqa: F811
    bot, api, runner, _ = setup(api=API(), runner=FakeRunner("ok\nREACT: 🔥"))
    run(bot.handle_message(msg("/react off", thread=3)))
    run(bot.handle_message(m(7, thread=3)))
    assert api.set == [] and "[message" not in runner.calls[0]["prompt"] and "REACT" not in api.sent[-1]["text"]
    run(bot.handle_message(m(8)))
    assert api.set
    run(bot.handle_message(msg("/react on", thread=3)))
    run(bot.handle_message(m(9, thread=3)))
    assert (9, "🔥") in api.set


def test_a_refused_reaction_changes_nothing_else(setup):  # noqa: F811
    bot, api, *_ = setup(api=API(fail=True))
    run(bot.handle_message(m(7)))
    assert len(api.sent) == 1


def test_cancel_takes_the_status_away(setup):  # noqa: F811
    bot, api, runner, _ = setup(api=API(), runner=FakeRunner(delay=5))

    async def go():
        t = asyncio.create_task(bot.handle_message(m(7)))
        await asyncio.sleep(0.1)
        await bot.handle_message(m(8, "/cancel"))
        await t

    run(go())
    assert api.set == [(7, "👀"), (7, "")]


def test_only_the_latest_are_kept(setup):  # noqa: F811
    *_, store = setup()
    for i in range(1, 20):
        store.add_owner_message("personal", CHAT, 0, i, keep=5)
    assert not store.is_owner_message("personal", CHAT, 0, 14) and store.is_owner_message("personal", CHAT, 0, 15)
