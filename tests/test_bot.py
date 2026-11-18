import asyncio
from pathlib import Path

import pytest

from assistant_bridge.bot import ProfileBot
from assistant_bridge.claude_runner import RunResult
from assistant_bridge.config import Profile
from assistant_bridge.store import Store
from assistant_bridge.telegram import TelegramError

OWNER = 42
CHAT = 42


class FakeAPI:
    def __init__(self, topics=True):
        self.topics = topics
        self.sent: list[dict] = []
        self.files_sent: list[tuple[Path, int | None, bool]] = []
        self.downloads: dict[str, bytes] = {}
        self.reject_html = False

    async def get_me(self):
        return {"username": "test_bot"}

    async def set_my_commands(self, commands):
        pass

    async def send_message(self, chat_id, text, thread_id=None, parse_mode=None):
        if parse_mode == "HTML" and self.reject_html:
            raise TelegramError("sendMessage", "can't parse entities", 400)
        self.sent.append({"chat_id": chat_id, "text": text, "thread_id": thread_id, "parse_mode": parse_mode})
        return {}

    async def send_chat_action(self, chat_id, action, thread_id=None):
        pass

    async def create_forum_topic(self, chat_id, name):
        if not self.topics:
            raise TelegramError("createForumTopic", "the chat is not a forum", 400)
        return {"message_thread_id": 555, "name": name}

    async def get_file(self, file_id):
        return {"file_path": f"files/{file_id}"}

    async def download_file(self, file_path, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.downloads.get(file_path, b"data"))

    async def send_file(self, chat_id, path, thread_id=None, as_photo=False):
        self.files_sent.append((path, thread_id, as_photo))
        return {}


class FakeRunner:
    def __init__(self, reply="Hi **there**", ok=True, delay=0.0):
        self.reply = reply
        self.ok = ok
        self.delay = delay
        self.calls: list[dict] = []

    async def run_turn(self, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.ok:
            return RunResult(ok=True, text=self.reply, cost_usd=0.01)
        return RunResult(ok=False, text="", error="usage limit reached")


@pytest.fixture
def setup(tmp_path):
    def make(api=None, runner=None):
        profile = Profile(
            name="personal",
            bot_token_env="TG",
            bot_token="t",
            owner_ids=frozenset({OWNER}),
            workspace=tmp_path / "ws",
            model="sonnet",
            allowed_tools=("Read",),
            extra_args=(),
        )
        profile.workspace.mkdir()
        api = api or FakeAPI()
        runner = runner or FakeRunner()
        store = Store(":memory:")
        return ProfileBot(profile, api, store, runner), api, runner, store

    return make


def msg(text=None, user=OWNER, thread=None, **extra):
    m = {"message_id": 1, "chat": {"id": CHAT, "type": "private"}, "from": {"id": user}, **extra}
    if text is not None:
        m["text"] = text
    if thread is not None:
        m["message_thread_id"] = thread
        m["is_topic_message"] = True
    return m


def run(coro):
    return asyncio.run(coro)


def test_unauthorized_user_is_ignored(setup):
    bot, api, runner, _ = setup()
    run(bot.handle_message(msg("hello", user=999)))
    assert runner.calls == [] and api.sent == []


def test_owner_message_runs_claude_and_replies_with_html(setup):
    bot, api, runner, store = setup()
    run(bot.handle_message(msg("hello")))
    (call,) = runner.calls
    assert call["prompt"] == "hello"
    assert call["started"] is False and call["model"] == "sonnet"
    assert api.sent[-1]["text"] == "Hi <b>there</b>"
    assert api.sent[-1]["parse_mode"] == "HTML"
    assert store.get_conversation("personal", CHAT, 0).started

    run(bot.handle_message(msg("again")))
    assert runner.calls[1]["started"] is True
    assert runner.calls[1]["session_id"] == call["session_id"]


def test_topics_get_separate_sessions_and_replies_go_to_the_topic(setup):
    bot, api, runner, _ = setup()
    run(bot.handle_message(msg("in main")))
    run(bot.handle_message(msg("in topic", thread=7)))
    assert runner.calls[0]["session_id"] != runner.calls[1]["session_id"]
    assert api.sent[-1]["thread_id"] == 7


def test_html_rejected_falls_back_to_plain_text(setup):
    api = FakeAPI()
    api.reject_html = True
    bot, api, _, _ = setup(api=api)
    run(bot.handle_message(msg("hello")))
    assert api.sent[-1] == {"chat_id": CHAT, "text": "Hi **there**", "thread_id": 0, "parse_mode": None}


def test_failed_run_reports_error(setup):
    bot, api, _, store = setup(runner=FakeRunner(ok=False))
    run(bot.handle_message(msg("hello")))
    assert "usage limit reached" in api.sent[-1]["text"]
    assert store.run_stats("personal", CHAT, 0).failed == 1


def test_new_creates_topic(setup):
    bot, api, _, store = setup()
    run(bot.handle_message(msg("/new Trip planning")))
    assert store.get_conversation("personal", CHAT, 555).title == "Trip planning"
    assert api.sent[-1]["thread_id"] == 555


def test_new_without_topics_resets_current_chat(setup):
    bot, api, _, store = setup(api=FakeAPI(topics=False))
    before = store.get_or_create("personal", CHAT, 0).session_id
    run(bot.handle_message(msg("/new")))
    assert store.get_conversation("personal", CHAT, 0).session_id != before
    assert "BotFather" in api.sent[-1]["text"]


def test_model_command(setup):
    bot, api, runner, _ = setup()
    run(bot.handle_message(msg("/model opus")))
    run(bot.handle_message(msg("hi")))
    assert runner.calls[-1]["model"] == "opus"
    run(bot.handle_message(msg("/model default")))
    run(bot.handle_message(msg("hi")))
    assert runner.calls[-1]["model"] == "sonnet"


def test_unknown_slash_command_goes_to_claude(setup):
    bot, _, runner, _ = setup()
    run(bot.handle_message(msg("/summarize-inbox")))
    assert runner.calls[-1]["prompt"] == "/summarize-inbox"


def test_photo_is_downloaded_into_inbox(setup):
    bot, _, runner, _ = setup()
    photo = [{"file_id": "small", "file_unique_id": "s"}, {"file_id": "big", "file_unique_id": "b", "file_size": 10}]
    run(bot.handle_message(msg(caption="what is this?", photo=photo)))
    prompt = runner.calls[-1]["prompt"]
    assert prompt.startswith("what is this?")
    rel = prompt.split("- ", 1)[1].split(" (image)")[0]
    assert rel.startswith("inbox/") and (bot.profile.workspace / rel).is_file()


def test_too_large_file_is_not_downloaded(setup):
    bot, _, runner, _ = setup()
    doc = {"file_id": "f", "file_name": "huge.zip", "file_size": 100_000_000}
    run(bot.handle_message(msg(document=doc)))
    assert "too large" in runner.calls[-1]["prompt"]
    assert not (bot.profile.workspace / "inbox").exists()


def test_send_file_marker_sends_workspace_files_only(setup):
    reply = "Here you go\nSEND_FILE: out/report.pdf\nSEND_FILE: ../secret.txt\nSEND_FILE: chart.png"
    bot, api, _, _ = setup(runner=FakeRunner(reply=reply))
    ws = bot.profile.workspace
    (ws / "out").mkdir()
    (ws / "out" / "report.pdf").write_bytes(b"%PDF")
    (ws / "chart.png").write_bytes(b"png")
    (ws.parent / "secret.txt").write_text("no")
    run(bot.handle_message(msg("make a report")))
    assert api.sent[0]["text"] == "Here you go"
    sent = [(p.name, photo) for p, _, photo in api.files_sent]
    assert sent == [("report.pdf", False), ("chart.png", True)]
    assert any("outside the workspace" in m["text"] for m in api.sent)


def test_cancel_stops_running_task(setup):
    bot, api, _, store = setup(runner=FakeRunner(delay=5))

    async def scenario():
        task = asyncio.create_task(bot.handle_message(msg("long research")))
        await asyncio.sleep(0.1)
        await bot.handle_message(msg("/cancel"))
        await asyncio.wait_for(task, 2)

    run(scenario())
    assert api.sent[-1]["text"] == "⏹ Stopped."
    assert store.run_stats("personal", CHAT, 0).runs == 1


def test_messages_in_one_conversation_run_in_order(setup):
    bot, _, runner, _ = setup(runner=FakeRunner(delay=0.05))

    async def scenario():
        await asyncio.gather(*(bot.handle_message(msg(f"m{i}")) for i in range(3)))

    run(scenario())
    assert [c["prompt"] for c in runner.calls] == ["m0", "m1", "m2"]
