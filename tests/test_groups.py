import asyncio

import pytest
from test_bot import OWNER, FakeAPI, FakeRunner, msg, setup  # noqa: F401 (setup is a fixture)

from assistant_bridge.bot import ProfileBot
from assistant_bridge.config import ConfigError, Group, Profile, load_config
from assistant_bridge.store import Store

GROUP = -1001234


@pytest.fixture
def gsetup(tmp_path):
    def make(runner=None, fresh=True, batch=0.05):
        profile = Profile(
            name="personal",
            bot_token_env="TG",
            bot_token="t",
            owner_ids=frozenset({OWNER}),
            workspace=tmp_path / "ws",
            model="sonnet",
            allowed_tools=("Read",),
            extra_args=(),
            groups=(Group(chat_id=GROUP, name="clipboard", batch_seconds=batch, fresh_session=fresh),),
        )
        profile.workspace.mkdir()
        api = FakeAPI()
        runner = runner or FakeRunner(reply="Saved.")
        store = Store(":memory:")
        bot = ProfileBot(profile, api, store, runner)
        bot.username = "test_bot"
        return bot, api, runner, store

    return make


def gmsg(text=None, user=OWNER, chat=GROUP, **extra):
    m = {"message_id": 1, "chat": {"id": chat, "type": "supergroup", "title": "Clipboard"}, "from": {"id": user}}
    m.update(extra)
    if text is not None:
        m["text"] = text
    return m


async def settle(bot):
    while bot._tasks:
        await asyncio.gather(*list(bot._tasks), return_exceptions=True)


def feed(bot, *messages, gap=0.0):
    async def go():
        for m in messages:
            await bot.handle_message(m)
            if gap:
                await asyncio.sleep(gap)
        await settle(bot)

    asyncio.run(go())


def test_group_message_runs_with_group_instructions(gsetup):
    bot, api, runner, _ = gsetup()
    (bot.profile.workspace / "groups").mkdir()
    (bot.profile.workspace / "groups" / "clipboard.md").write_text("Tag every item #idea.", encoding="utf-8")
    feed(bot, gmsg("cool article https://example.com"))
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert "cool article" in call["prompt"]
    assert "Tag every item #idea." in call["append_system_prompt"]
    assert 'group "Clipboard"' in call["append_system_prompt"]
    assert "NO_REPLY" in call["append_system_prompt"]
    assert api.sent[-1]["chat_id"] == GROUP
    assert api.sent[-1]["text"] == "Saved."


def test_missing_instructions_file_uses_default(gsetup):
    bot, _, runner, _ = gsetup()
    feed(bot, gmsg("x"))
    assert "groups/clipboard.md is missing" in runner.calls[0]["append_system_prompt"]


def test_burst_is_batched_into_one_run(gsetup):
    bot, _, runner, _ = gsetup(batch=0.2)
    feed(bot, gmsg("one"), gmsg("two"), gmsg("three"), gap=0.01)
    assert len(runner.calls) == 1
    prompt = runner.calls[0]["prompt"]
    assert "Item 1 of 3" in prompt and "Item 3 of 3" in prompt
    assert prompt.index("one") < prompt.index("two") < prompt.index("three")


def test_message_during_run_starts_new_batch_without_cancelling(gsetup):
    bot, api, runner, _ = gsetup(runner=FakeRunner(reply="ok", delay=0.2), batch=0.02)
    feed(bot, gmsg("first"), gmsg("second"), gap=0.1)
    assert [c["prompt"] for c in runner.calls] == ["first", "second"]
    assert not any("Stopped" in s["text"] for s in api.sent)


def test_unconfigured_group_is_ignored(gsetup, caplog):
    bot, api, runner, _ = gsetup()
    with caplog.at_level("WARNING"):
        feed(bot, gmsg("hi", chat=-999))
    assert runner.calls == [] and api.sent == []
    assert "chat_id=-999" in caplog.text


def test_non_owner_in_group_is_ignored(gsetup):
    bot, api, runner, _ = gsetup()
    feed(bot, gmsg("hi", user=7))
    assert runner.calls == [] and api.sent == []


def test_no_reply_sends_nothing(gsetup):
    bot, api, _, _ = gsetup(runner=FakeRunner(reply="NO_REPLY"))
    feed(bot, gmsg("meh"))
    assert api.sent == []


def test_no_reply_is_shown_in_private_chat(setup):
    # Only groups may stay silent; in a private chat the owner always gets an answer.
    bot, api, _, _ = setup(runner=FakeRunner(reply="NO_REPLY"))

    asyncio.run(bot.handle_message(msg("hi")))
    assert api.sent[-1]["text"] == "NO_REPLY"


def test_fresh_session_per_batch(gsetup):
    bot, _, runner, _ = gsetup(fresh=True)
    feed(bot, gmsg("a"))
    feed(bot, gmsg("b"))
    first, second = runner.calls
    assert first["session_id"] != second["session_id"]
    assert first["started"] is False and second["started"] is False


def test_ongoing_session_when_fresh_disabled(gsetup):
    bot, _, runner, _ = gsetup(fresh=False)
    feed(bot, gmsg("a"))
    feed(bot, gmsg("b"))
    first, second = runner.calls
    assert first["session_id"] == second["session_id"]
    assert second["started"] is True


def test_forward_origin_and_hidden_links_in_prompt(gsetup):
    bot, _, runner, _ = gsetup()
    forwarded = gmsg(
        "Read this",
        forward_origin={
            "type": "channel",
            "chat": {"id": -100, "title": "Tech News", "username": "technews"},
            "message_id": 77,
            "date": 1700000000,
        },
        entities=[{"type": "text_link", "offset": 0, "length": 4, "url": "https://example.com/a"}],
    )
    feed(bot, forwarded)
    prompt = runner.calls[0]["prompt"]
    assert "[Forwarded from Tech News (@technews), originally posted 2023-11-14 22:13 UTC" in prompt
    assert "https://t.me/technews/77" in prompt
    assert "- https://example.com/a" in prompt


def test_forwarded_sticker_alone_is_skipped(gsetup):
    bot, api, runner, _ = gsetup()
    feed(bot, gmsg(sticker={"file_id": "s"}, forward_origin={"type": "hidden_user", "sender_user_name": "X"}))
    assert runner.calls == [] and api.sent == []


def test_forwarded_text_starting_with_slash_is_not_a_command(gsetup):
    bot, _, runner, _ = gsetup()
    feed(bot, gmsg("/status is a nice page", forward_origin={"type": "hidden_user", "sender_user_name": "X"}))
    assert len(runner.calls) == 1


def test_commands_in_group(gsetup):
    bot, api, runner, _ = gsetup()
    feed(bot, gmsg("/status@other_bot"))
    assert api.sent == []
    feed(bot, gmsg("/status@test_bot"))
    assert "Profile: personal" in api.sent[-1]["text"]
    feed(bot, gmsg("/new"))
    assert api.sent[-1]["text"] == "Started a fresh conversation here."
    assert runner.calls == []


def test_group_upgrade_is_logged(gsetup, caplog):
    bot, _, runner, _ = gsetup()
    with caplog.at_level("WARNING"):
        feed(bot, gmsg(migrate_to_chat_id=-100777))
    assert "-100777" in caplog.text and runner.calls == []


def _write_config(tmp_path, groups_toml):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[[profiles]]\nname = "personal"\nbot_token_env = "TG"\nowner_ids = [1]\n' + groups_toml,
        encoding="utf-8",
    )
    return cfg


def test_config_parses_groups(tmp_path):
    cfg = _write_config(
        tmp_path, '[[profiles.groups]]\nchat_id = -100123\nname = "clipboard"\nbatch_seconds = 3\n'
    )
    profile = load_config(cfg, {"TG": "1:abc"}).profiles[0]
    assert profile.groups == (Group(chat_id=-100123, name="clipboard", batch_seconds=3.0, fresh_session=True),)
    assert profile.group(-100123) is profile.groups[0]
    assert profile.group(5) is None


@pytest.mark.parametrize(
    "toml",
    [
        '[[profiles.groups]]\nchat_id = 123\nname = "x"\n',  # positive id is a user, not a group
        '[[profiles.groups]]\nchat_id = -1\nname = "Bad Name"\n',
        '[[profiles.groups]]\nchat_id = -1\nname = "x"\nbatch_seconds = 999\n',
        '[[profiles.groups]]\nchat_id = -1\nname = "x"\nfresh_session = "yes"\n',
        '[[profiles.groups]]\nchat_id = -1\nname = "x"\n[[profiles.groups]]\nchat_id = -1\nname = "y"\n',
    ],
)
def test_config_rejects_bad_groups(tmp_path, toml):
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, toml), {"TG": "1:abc"})
