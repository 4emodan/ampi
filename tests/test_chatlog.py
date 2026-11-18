import asyncio
from datetime import datetime, timezone

from test_bot import FakeRunner, msg, setup  # noqa: F401 (setup is a fixture)
from test_groups import feed, gmsg, gsetup  # noqa: F401 (gsetup is a fixture)

from assistant_bridge import chatlog
from assistant_bridge.bot import bridge_system_prompt


def read_logs(folder):
    return "".join(p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md")))


def test_private_chat_is_logged(setup):
    bot, _, _, _ = setup(runner=FakeRunner(reply="Hello back"))
    asyncio.run(bot.handle_message(msg("hello", date=1763452800)))  # 2025-11-18 08:00 UTC
    folder = bot.profile.workspace / "chats" / "private" / "main"
    assert "### 08:00 UTC · Owner\nhello\n" in (folder / "2025-11-18.md").read_text(encoding="utf-8")
    text = read_logs(folder)  # the reply is stamped with today's date, so it may be in another file
    assert "· Assistant\nHello back\n" in text
    assert text.index("Owner") < text.index("Assistant")


def test_topic_log_folder_uses_title(setup):
    bot, _, _, store = setup()
    store.get_or_create("personal", 42, 555, title="Trip planning!")
    asyncio.run(bot.handle_message(msg("hi", thread=555)))
    assert list((bot.profile.workspace / "chats" / "private").iterdir())[0].name == "topic-555-trip-planning"


def test_group_batch_logs_each_item_and_skips_no_reply(gsetup):
    bot, _, _, _ = gsetup(runner=FakeRunner(reply="NO_REPLY"))
    feed(bot, gmsg("one"), gmsg("two"))
    text = read_logs(bot.profile.workspace / "chats" / "clipboard")
    assert text.count("· Owner") == 2
    assert "one" in text and "two" in text
    assert "Assistant" not in text


def test_errors_are_logged(setup):
    bot, _, _, _ = setup(runner=FakeRunner(ok=False))
    asyncio.run(bot.handle_message(msg("hi")))
    text = read_logs(bot.profile.workspace / "chats" / "private" / "main")
    assert "⚠️ usage limit reached" in text


def test_commands_are_not_logged(setup):
    bot, _, _, _ = setup()
    asyncio.run(bot.handle_message(msg("/status")))
    assert not (bot.profile.workspace / "chats").exists()


def test_system_prompt_lists_groups_and_logs(gsetup):
    bot, _, runner, _ = gsetup()
    prompt = bridge_system_prompt(bot.profile)
    assert "chats/private/main/" in prompt
    assert 'chats/clipboard/ – the Telegram group "clipboard"' in prompt
    assert "groups/clipboard.md" in prompt
    feed(bot, gmsg("x"))
    assert "chats/clipboard/" in runner.calls[0]["append_system_prompt"]


def test_append_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    chatlog.append(blocker / "sub", "Owner", "hi", datetime(2026, 1, 1, tzinfo=timezone.utc))  # no exception
