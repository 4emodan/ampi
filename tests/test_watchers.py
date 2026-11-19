import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from test_bot import OWNER, FakeAPI, FakeRunner, setup  # noqa: F401 (setup is a fixture)

from assistant_bridge.approvals import ApprovalRelay
from assistant_bridge.config import ConfigError, Tools, Watcher, load_config
from assistant_bridge.services import ServiceError
from assistant_bridge.watchers import FAILS_BEFORE_NOTICE, Watch


def run(coro):
    return asyncio.run(coro)


class ButtonAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.buttons, self.edits, self.answers = [], [], []

    async def send_buttons(self, chat_id, text, buttons):
        self.buttons.append({"chat_id": chat_id, "text": text, "buttons": buttons})
        return {"message_id": 77}

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    async def answer_callback_query(self, cq_id, text=None):
        self.answers.append(text)


class FakeTools:
    """Stands in for the ampi-tools and tg-reader sockets."""

    def __init__(self):
        self.requests = []
        self.pending = []
        self.ids = []
        self.down = False
        self.tg = {"count": 0, "last_id": 10, "text": "", "chat": "Saved"}

    async def __call__(self, path, payload):
        self.requests.append((path, payload))
        if self.down:
            raise ServiceError("service at x is not reachable")
        cmd = payload["cmd"]
        if cmd == "approvals.pending":
            items, self.pending = self.pending, []
            return {"ok": True, "items": items}
        if cmd == "approvals.notified":
            return {"ok": True}
        if cmd == "approvals.decide":
            return {"ok": True, "status": "done" if payload["approve"] else "rejected", "text": "Completed 1."}
        if cmd == "since":
            return {"ok": True, **self.tg}
        op = payload["ops"][0]["op"]
        if op.endswith(".ids"):
            return {"ok": True, "ids": list(self.ids)}
        return {"ok": True, "text": "\n".join(f"- [{i}] item" for i in payload["ops"][0]["args"]["ids"])}


# --- approvals ----------------------------------------------------------------------------


def test_requests_become_buttons_and_answers_go_back(setup):
    bot, _, _, _ = setup(api=ButtonAPI())
    tools = FakeTools()
    relay = ApprovalRelay(bot, "sock", tools)
    tools.pending = [{"id": 5, "summary": "• Complete “Buy milk”", "reason": "done already"}]
    run(relay.tick())
    (b,) = bot.api.buttons
    assert b["chat_id"] == OWNER and "Approve? (request #5)" in b["text"] and "Why: done already" in b["text"]
    assert b["buttons"][0][0]["callback_data"] == "ap:5:y"
    assert ("sock", {"cmd": "approvals.notified", "id": 5}) in tools.requests

    cq = {"id": "c1", "from": {"id": OWNER}, "data": "ap:5:y",
          "message": {"message_id": 77, "chat": {"id": OWNER}, "text": b["text"]}}
    run(relay.handle_callback(cq))
    assert ("sock", {"cmd": "approvals.decide", "id": 5, "approve": True}) in tools.requests
    (edit,) = bot.api.edits
    assert edit[1] == 77 and edit[2].endswith("✅ Done\nCompleted 1.")
    log = (bot.profile.workspace / "chats" / "private" / "main").glob("*.md")
    assert "Approve? (request #5)" in "".join(p.read_text(encoding="utf-8") for p in log)


def test_strangers_cannot_press_the_buttons(setup):
    bot, _, _, _ = setup(api=ButtonAPI())
    tools = FakeTools()
    run(ApprovalRelay(bot, "sock", tools).handle_callback(
        {"id": "c", "from": {"id": 999}, "data": "ap:5:y", "message": {"message_id": 1}}))
    assert tools.requests == [] and bot.api.edits == []


# --- watchers -----------------------------------------------------------------------------


def gmail_watch(bot, tools, **kw):
    w = Watcher(name="inbox", kind="gmail", query="in:inbox", every_minutes=10, **kw)
    bot.profile = replace(bot.profile, watchers=(w,))
    return Watch(bot, w, Tools(socket="tools", tg_reader_socket="tg"), tools)


def test_first_check_is_a_baseline_then_new_items_run_claude(setup):
    bot, api, runner, _ = setup(runner=FakeRunner(reply="Archived 2 newsletters."))
    tools = FakeTools()
    tools.ids = ["a", "b"]
    watch = gmail_watch(bot, tools)
    run(watch.tick())
    assert runner.calls == []  # the backlog isn't handled

    tools.ids = ["c", "a", "b"]
    run(watch.tick())
    (call,) = runner.calls
    assert 'Watcher "inbox": 1 new email(s)' in call["prompt"] and "- [c] item" in call["prompt"]
    assert 'started by your watcher "inbox"' in call["append_system_prompt"]
    assert call["started"] is False
    assert api.sent[-1]["chat_id"] == OWNER and api.sent[-1]["text"].startswith("🔔 <b>inbox</b>")
    main_log = "".join(p.read_text(encoding="utf-8") for p in
                       (bot.profile.workspace / "chats" / "private" / "main").glob("*.md"))
    assert "Assistant (watcher inbox)" in main_log

    run(watch.tick())  # nothing new
    assert len(runner.calls) == 1


def test_no_reply_stays_silent(setup):
    bot, api, runner, _ = setup(runner=FakeRunner(reply="NO_REPLY"))
    tools = FakeTools()
    watch = gmail_watch(bot, tools)
    run(watch.tick())
    tools.ids = ["x"]
    run(watch.tick())
    assert len(runner.calls) == 1 and api.sent == []


def test_owner_hears_about_a_broken_watcher_once(setup):
    bot, api, _, _ = setup()
    tools = FakeTools()
    tools.down = True
    watch = gmail_watch(bot, tools)
    for _ in range(FAILS_BEFORE_NOTICE + 2):
        run(watch.tick())
    assert len(api.sent) == 1 and "Watcher inbox can't check gmail" in api.sent[0]["text"]
    tools.down = False
    run(watch.tick())
    assert "works again" in api.sent[-1]["text"]


def test_telegram_watcher_uses_last_id(setup):
    bot, _, runner, _ = setup()
    tools = FakeTools()
    w = Watcher(name="clip", kind="telegram", query="Clipboard", every_minutes=5)
    watch = Watch(bot, w, Tools(socket="tools", tg_reader_socket="tg"), tools)
    run(watch.tick())
    assert tools.requests[-1] == ("tg", {"cmd": "since", "chat": "Clipboard", "after_id": None})
    tools.tg = {"count": 2, "last_id": 12, "text": "### msg 11\n\n### msg 12", "chat": "Clipboard"}
    run(watch.tick())
    assert tools.requests[-1][1]["after_id"] == 10
    assert "2 new message(s) in the Telegram chat \"Clipboard\"" in runner.calls[0]["prompt"]
    assert bot.store.get_watch_state("personal", "clip") == {"last_id": 12}


def test_schedule_fires_once_per_slot(setup):
    bot, _, runner, _ = setup()
    bot.profile = replace(bot.profile, timezone="Europe/Athens")  # UTC+2 in November
    now = [datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)]  # 07:00 local
    w = Watcher(name="morning", kind="schedule", query="", every_minutes=10, at=("08:00",))
    watch = Watch(bot, w, Tools(), FakeTools(), clock=lambda: now[0])
    run(watch.tick())  # baseline: yesterday's 08:00 counts as done
    assert runner.calls == []
    now[0] = datetime(2026, 11, 20, 6, 1, tzinfo=timezone.utc)  # 08:01 local
    run(watch.tick())
    run(watch.tick())
    (call,) = runner.calls
    assert 'Scheduled run "morning" for Friday 2026-11-20 08:00 (Europe/Athens)' in call["prompt"]


def test_schedule_skips_runs_missed_by_hours_and_respects_days(setup):
    bot, _, runner, store = setup()
    w = Watcher(name="m", kind="schedule", query="", every_minutes=10, at=("08:00",), days=("sat", "sun"))
    clock = [datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)]  # Friday
    watch = Watch(bot, w, Tools(), FakeTools(), clock=lambda: clock[0])
    store.set_watch_state("personal", "m", {"last": "2026-11-01 08:00"})
    run(watch.tick())
    assert runner.calls == []  # Friday isn't a chosen day
    clock[0] = datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc)  # Saturday, 5 hours late
    run(watch.tick())
    assert runner.calls == [] and store.get_watch_state("personal", "m") == {"last": "2026-11-21 08:00"}


# --- config -------------------------------------------------------------------------------


def write_config(tmp_path, extra):
    p = tmp_path / "config.toml"
    p.write_text(
        'timezone_unused = 1\n[tools]\napprovals = true\n[[profiles]]\nname = "personal"\n'
        'bot_token_env = "TG"\nowner_ids = [42]\ntimezone = "Europe/Athens"\n' + extra,
        encoding="utf-8",
    )
    return p


def test_config_parses_watchers_and_tools(tmp_path):
    cfg = load_config(write_config(tmp_path, (
        '[[profiles.watchers]]\nname = "inbox"\nkind = "gmail"\nquery = "in:inbox"\nevery_minutes = 15\n'
        '[[profiles.watchers]]\nname = "brief"\nkind = "schedule"\nat = ["08:00", "18:30"]\ndays = ["Mon", "fri"]\n'
    )), {"TG": "t"})
    assert cfg.tools.approvals and cfg.tools.socket == "/run/ampi-tools/tools.sock"
    inbox, brief = cfg.profiles[0].watchers
    assert inbox.every_minutes == 15 and brief.at == ("08:00", "18:30") and brief.days == ("mon", "fri")
    assert cfg.profiles[0].timezone == "Europe/Athens" and cfg.profiles[0].owner_chat_id == 42


@pytest.mark.parametrize("bad", [
    '[[profiles.watchers]]\nname = "x"\nkind = "fax"\nquery = "q"\n',
    '[[profiles.watchers]]\nname = "x"\nkind = "gmail"\n',
    '[[profiles.watchers]]\nname = "x"\nkind = "schedule"\nat = ["8am"]\n',
    '[[profiles.watchers]]\nname = "x"\nkind = "schedule"\n',
])
def test_config_rejects_bad_watchers(tmp_path, bad):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad), {"TG": "t"})
