"""Telegram writes as the owner: tg-reader's writer, ampi-tools' permissions and one-time requests, the
bridge's relay and run registration."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from ampi_tools import client as cli
from ampi_tools import ops_telegram as tg
from ampi_tools.ops import Approvals, Policy, Services
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError
from tg_reader.reader import Reader
from tg_reader.writer import Writer

BRIDGE = 4242
ME = 1000
NOW = datetime.now(timezone.utc)


def run(coro):
    return asyncio.run(coro)


# --- a fake Telethon --------------------------------------------------------------------------------

ANNA = NS(id=11, first_name="Anna", username="anna_k", bot=False, contact=True)
FRIENDS = NS(id=22, title="Friends", username=None, participants_count=12, bot=False)
BIG = NS(id=33, title="Big group", username=None, participants_count=5000, bot=False)
NEWS = NS(id=44, title="News", username="newsch", broadcast=True, bot=False, admin_rights=True)
AMPI_BOT = NS(id=55, first_name="Ampi", username="ampi_bot", bot=True)
LOGIN = NS(id=777000, first_name="Telegram", username=None, bot=False)
TWIN1 = NS(id=66, first_name="Mom", username=None, bot=False)
TWIN2 = NS(id=67, first_name="Mom", username=None, bot=False)


def dialog(entity, name, kind):
    is_user = kind in ("person", "bot")
    return NS(entity=entity, name=name, id=entity.id if is_user else -1000000000000 - entity.id, is_user=is_user,
              is_group=kind == "group", is_channel=kind in ("group", "channel"), unread_count=0, date=NOW)


DIALOGS = [dialog(ANNA, "Anna K", "person"), dialog(FRIENDS, "Friends", "group"), dialog(BIG, "Big group", "group"),
           dialog(NEWS, "News", "channel"), dialog(AMPI_BOT, "Ampi", "bot"), dialog(LOGIN, "Telegram", "person"),
           dialog(TWIN1, "Mom", "person"), dialog(TWIN2, "Mom", "person")]
FRIENDS_ID = DIALOGS[1].id
BIG_ID = DIALOGS[2].id
NEWS_ID = DIALOGS[3].id


def msg(i, text, out=True, minutes_ago=5, media=None, edit_date=None):
    return NS(id=i, message=text, out=out, date=NOW - timedelta(minutes=minutes_ago), edit_date=edit_date,
              action=None, media=media, photo=media, voice=None, video_note=None, video=None, gif=None, sticker=None,
              poll=None, document=None, web_preview=None, forward=None, reply_to_msg_id=None, entities=None,
              sender=ANNA, chat=ANNA, chat_id=None)


class FakeTelethon:
    def __init__(self):
        self.dialogs = DIALOGS
        self.messages: dict[int, dict[int, NS]] = {ANNA.id: {5: msg(5, "hi Anna"), 6: msg(6, "from Anna", out=False),
                                                              7: msg(7, "old", minutes_ago=60 * 72),
                                                              8: msg(8, "", media=object())}}
        self.sent, self.edited, self.deleted = [], [], []
        self.next_id = 100
        self.fail: Exception | None = None
        self.delete_count = 1

    async def iter_dialogs(self, limit=None):
        for d in self.dialogs:
            yield d

    def _peer(self, entity):
        return ME if entity == "me" else getattr(entity, "id", entity)

    async def get_messages(self, entity, ids):
        box = self.messages.get(self._peer(entity), {})
        return [box.get(i) for i in ids]

    async def send_message(self, entity, text, reply_to=None, parse_mode=(), link_preview=True):
        assert parse_mode is None and link_preview is False
        if self.fail:
            raise self.fail
        self.next_id += 1
        m = msg(self.next_id, text, minutes_ago=0)
        self.messages.setdefault(self._peer(entity), {})[m.id] = m
        self.sent.append((self._peer(entity), text, reply_to))
        return m

    async def edit_message(self, entity, mid, text, parse_mode=(), link_preview=True):
        assert parse_mode is None
        if self.fail:
            raise self.fail
        m = self.messages[self._peer(entity)][mid]
        m.message, m.edit_date = text, datetime.now(timezone.utc)
        self.edited.append((self._peer(entity), mid, text))
        return m

    async def delete_messages(self, entity, ids, revoke=False):
        assert revoke is True
        self.deleted.append((self._peer(entity), ids))
        return [NS(pts_count=self.delete_count)]


class PeerFloodError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__("wait")
        self.seconds = seconds


class SlowModeWaitError(Exception):
    def __init__(self, seconds):
        super().__init__("slow")
        self.seconds = seconds


def make_writer(tmp_path, client=None):
    client = client or FakeTelethon()
    reader = Reader(client)
    reader.self_id = ME
    return Writer(reader, ME, tmp_path / "state.json"), client


PROTECTED = [999]


def w(writer, **payload):
    return run(writer.handle({"protected": PROTECTED, **payload}))


# --- the writer (inside tg-reader) -----------------------------------------------------------------

def test_chats_are_found_exactly_and_only_in_the_chat_list(tmp_path):
    writer, _ = make_writer(tmp_path)
    assert w(writer, cmd="resolve", chat="Anna K")["peer_id"] == ANNA.id
    assert w(writer, cmd="resolve", chat="@anna_k")["kind"] == "person"
    assert w(writer, cmd="resolve", chat="me")["kind"] == "saved"
    assert w(writer, cmd="resolve", chat=str(FRIENDS_ID))["members"] == 12
    partial = w(writer, cmd="resolve", chat="Anna")
    assert not partial["ok"] and "Anna K" in partial["error"]  # a hint, not a guess
    assert "2 chats are named" in w(writer, cmd="resolve", chat="Mom")["error"]
    assert "isn't in your chat list" in w(writer, cmd="resolve", chat="@stranger")["error"]
    assert "bots" in w(writer, cmd="resolve", chat="Ampi")["error"]
    assert "off limits" in w(writer, cmd="resolve", chat="777000")["error"]


def test_nothing_is_written_without_the_protect_list_or_into_protected_chats(tmp_path):
    writer, client = make_writer(tmp_path)
    assert "isn't available" in run(writer.handle({"cmd": "send", "peer_id": ANNA.id, "text": "hi"}))["error"]
    assert "reads that chat itself" in run(writer.handle({"cmd": "send", "peer_id": ANNA.id, "text": "hi",
                                                          "protected": [ANNA.id]}))["error"]
    assert "bots" in w(writer, cmd="send", peer_id=AMPI_BOT.id, text="hi")["error"]
    assert "isn't in your chat list" in w(writer, cmd="send", peer_id=12345, text="hi")["error"]
    assert client.sent == []


def test_sent_messages_are_marked_as_sent_by_ampi_even_after_a_restart(tmp_path):
    writer, client = make_writer(tmp_path)
    reply = w(writer, cmd="send", peer_id=ANNA.id, text="hello")
    assert reply["ok"] and client.sent == [(ANNA.id, "hello", None)]
    writer2, _ = make_writer(tmp_path, client)  # a new process reads the saved state
    assert (ANNA.id, reply["message_id"]) in writer2.reader.ampi_sent
    assert writer2.reader._by_ampi(ANNA.id, NS(id=reply["message_id"]))
    assert not writer2.reader._by_ampi(FRIENDS_ID, NS(id=reply["message_id"]))  # ids are per chat


def test_only_own_unchanged_text_messages_are_edited_or_deleted(tmp_path):
    writer, client = make_writer(tmp_path)
    (snap,) = w(writer, cmd="messages", peer_id=ANNA.id, ids=[5])["messages"]
    expect = {"5": snap["hash"]}
    assert "isn't yours" in w(writer, cmd="edit", peer_id=ANNA.id, message_id=6, text="x",
                               expect={"6": "x"})["error"]
    assert "attachment" in w(writer, cmd="edit", peer_id=ANNA.id, message_id=8, text="x",
                              expect={"8": w(writer, cmd="messages", peer_id=ANNA.id, ids=[8])["messages"][0]["hash"]}
                              )["error"]
    client.messages[ANNA.id][5].message = "changed meanwhile"
    assert "changed since" in w(writer, cmd="edit", peer_id=ANNA.id, message_id=5, text="new", expect=expect)["error"]
    (snap,) = w(writer, cmd="messages", peer_id=ANNA.id, ids=[5])["messages"]
    assert w(writer, cmd="edit", peer_id=ANNA.id, message_id=5, text="new", expect={"5": snap["hash"]})["ok"]
    client.delete_count = 0
    (snap,) = w(writer, cmd="messages", peer_id=ANNA.id, ids=[5])["messages"]
    assert "deleted nothing" in w(writer, cmd="delete", peer_id=ANNA.id, ids=[5], expect={"5": snap["hash"]})["error"]


def test_write_limits_and_spam_signals(tmp_path):
    writer, client = make_writer(tmp_path)
    for i in range(10):
        assert w(writer, cmd="send", peer_id=ANNA.id, text=f"m{i}")["ok"]
    assert "a minute" in w(writer, cmd="send", peer_id=ANNA.id, text="one more")["error"]

    writer, client = make_writer(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    client.fail = SlowModeWaitError(30)
    assert "slow mode" in w(writer, cmd="send", peer_id=FRIENDS_ID, text="x")["error"]
    client.fail = None
    assert w(writer, cmd="send", peer_id=FRIENDS_ID, text="y")["ok"]  # slow mode doesn't stop the account

    client.fail = PeerFloodError("spam")
    reply = w(writer, cmd="send", peer_id=ANNA.id, text="z")
    assert reply["blocked"] and "paused until" in reply["error"]
    client.fail = None
    writer2, _ = make_writer(tmp_path / "b", client)
    assert "paused" in w(writer2, cmd="send", peer_id=ANNA.id, text="later")["error"]  # survives a restart


def test_flood_waits_pause_writing_proportionally(tmp_path):
    writer, client = make_writer(tmp_path)
    client.fail = FloodWaitError(600)
    w(writer, cmd="send", peer_id=ANNA.id, text="x")
    assert 5900 <= writer.state["blocked_until"] - time.time() <= 6001


def test_a_busy_connection_means_nothing_was_sent(tmp_path, monkeypatch):
    writer, client = make_writer(tmp_path)
    monkeypatch.setattr("tg_reader.writer.LOCK_WAIT", 0.05)

    async def scenario():
        await writer.reader._lock.acquire()
        try:
            return await writer.handle({"cmd": "send", "peer_id": ANNA.id, "text": "x", "protected": PROTECTED})
        finally:
            writer.reader._lock.release()

    reply = run(scenario())
    assert reply["not_sent"] and "nothing was sent" in reply["error"] and client.sent == []


def test_distinct_chats_per_day_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr("tg_reader.writer.CHATS_PER_DAY", 1)
    writer, _ = make_writer(tmp_path)
    assert w(writer, cmd="send", peer_id=ANNA.id, text="a")["ok"]
    assert "different chats" in w(writer, cmd="send", peer_id=FRIENDS_ID, text="b")["error"]


# --- ampi-tools: where requests come from ------------------------------------------------------------

def fake_proc(root: Path, pid: int, ppid: int, start: int) -> None:
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start), "0"]
    (root / str(pid) / "stat").write_text(f"{pid} (proc name) {' '.join(fields)}\n")


def test_origin_comes_from_process_ancestry(tmp_path):
    proc = tmp_path / "proc"
    fake_proc(proc, BRIDGE, 1, 10)
    fake_proc(proc, 500, BRIDGE, 20)      # claude, private chat
    fake_proc(proc, 501, 500, 21)         # bash
    fake_proc(proc, 502, 501, 22)         # telegram client
    fake_proc(proc, 600, BRIDGE, 30)      # claude, group
    fake_proc(proc, 601, 600, 31)
    fake_proc(proc, 700, 1, 40)           # daemonized / reparented
    reg = tg.RunRegistry(proc)
    reg.register(500, "private")
    reg.register(600, "group:friends")
    assert reg.origin_of(502) == "private"
    assert reg.origin_of(601) == "group:friends"
    assert reg.origin_of(700) == "unknown"
    assert reg.origin_of(BRIDGE) == "unknown"
    fake_proc(proc, 500, BRIDGE, 99)      # pid reused by another process
    assert reg.origin_of(502) == "unknown"


# --- ampi-tools: deciding and running ----------------------------------------------------------------

class DirectWriter(tg.TgWriterClient):
    """Talks to a real Writer (with fake Telethon) instead of the socket."""

    def __init__(self, store, writer, unknown=False):
        super().__init__(store, "unused")
        self.writer, self.unknown = writer, unknown

    async def call(self, payload, timeout=60):
        payload = {**payload, "protected": self.store.protected()}
        if self.unknown and payload["cmd"] in ("send", "edit", "delete"):
            return {"ok": False, "unknown": True}
        return await self.writer.handle(payload)


@pytest.fixture
def tgd(tmp_path):
    def make(rules=None, policy=None, unknown=False):
        proc = tmp_path / "proc"
        fake_proc(proc, BRIDGE, 1, 10)
        fake_proc(proc, 500, BRIDGE, 20)
        fake_proc(proc, 501, 500, 21)
        fake_proc(proc, 600, BRIDGE, 30)
        fake_proc(proc, 601, 600, 31)
        fake_proc(proc, 700, 1, 40)
        writer, client = make_writer(tmp_path)
        store = tg.TelegramStore(tmp_path / "tg.sqlite3")
        side = tg.TelegramSide(DirectWriter(store, writer, unknown), store,
                               tg.TelegramRules.parse(rules) if rules else tg.TelegramRules())

        async def trusted(pid):
            return pid == BRIDGE

        daemon = Daemon(Services(telegram_side=side), Approvals(tmp_path / "a.sqlite3"), policy or Policy(), trusted,
                        tg.RunRegistry(proc))
        bridge(daemon, {"cmd": "runs.register", "pid": 500, "origin": "private"})
        bridge(daemon, {"cmd": "runs.register", "pid": 600, "origin": "group:friends"})
        bridge(daemon, {"cmd": "telegram.protect", "ids": [55, 999]})
        return daemon, client, store

    return make


def bridge(daemon, payload):
    return run(daemon.handle(payload, BRIDGE))


def tell(daemon, op, pid=501, **args):
    return run(daemon.handle({"cmd": "run", "ops": [{"op": op, "args": args}]}, pid))


def pending(daemon):
    return bridge(daemon, {"cmd": "approvals.pending"})


def decide(daemon, item, answer="y"):
    bridge(daemon, {"cmd": "approvals.notified", "id": item["id"], "message_id": 9})
    return bridge(daemon, {"cmd": "approvals.decide", "id": item["id"], "approve": answer != "n",
                           "always": answer == "a", "nonce": item["nonce"], "message_id": 9})


ALLOW_ANNA = {"chats": [{"chat": 11, "allow": ["send"]}]}


def test_without_permission_a_send_becomes_a_one_time_request(tgd):
    daemon, client, _ = tgd()
    reply = tell(daemon, "telegram.send", chat="Anna K", text="see you at noon")
    assert reply["approval"] and client.sent == []
    (item,) = pending(daemon)["items"]
    assert "AS YOU (your own account, not Ampi's bot)" in item["summary"] and "│ see you at noon" in item["summary"]
    assert "30 days" in item["grant_label"] and "Anna K" in item["grant_label"]
    assert decide(daemon, item)["status"] == "done"
    assert client.sent == [(ANNA.id, "see you at noon", None)]


def test_allowed_plain_text_from_the_private_chat_goes_out_and_the_owner_is_told(tgd):
    daemon, client, store = tgd(rules=ALLOW_ANNA)
    reply = tell(daemon, "telegram.send", chat="Anna K", text="see you at noon")
    assert reply["ok"] and "Sent as you" in reply["text"] and client.sent
    (notice,) = pending(daemon)["notices"]
    assert "without asking" in notice["text"] and "│ see you at noon" in notice["text"]
    bridge(daemon, {"cmd": "notices.ack", "id": notice["id"]})
    assert pending(daemon)["notices"] == []
    assert "automatic" in tell(daemon, "telegram.log")["text"]


@pytest.mark.parametrize("pid,text,chat", [
    (601, "see you at noon", "Anna K"),          # a group run
    (700, "see you at noon", "Anna K"),          # an unknown process
    (501, "look at example.com", "Anna K"),      # a link
    (501, "the code is 482913", "Anna K"),       # digits
    (501, "ask @someone", "Anna K"),             # a mention
    (501, "mail me at a@b.co", "Anna K"),
])
def test_anything_beyond_plain_text_from_the_private_chat_asks(tgd, pid, text, chat):
    daemon, client, _ = tgd(rules=ALLOW_ANNA)
    reply = tell(daemon, "telegram.send", pid=pid, chat=chat, text=text)
    assert reply.get("approval") and client.sent == []
    (item,) = pending(daemon)["items"]
    if pid != 501:
        assert item["summary"].startswith("⚠ Asked for by") and "grant_label" not in item


def test_channels_and_big_groups_always_ask_and_never_offer_always(tgd):
    rules = {"chats": [{"chat": NEWS_ID, "allow": ["send"]}, {"chat": BIG_ID, "allow": ["send"]}]}
    daemon, client, _ = tgd(rules=rules)
    for chat in ("News", "Big group"):
        assert tell(daemon, "telegram.send", chat=chat, text="hello all").get("approval")
    items = pending(daemon)["items"]
    assert all("grant_label" not in i for i in items)
    assert "PUBLIC to" in items[0]["summary"] or "PUBLIC channel" in items[0]["summary"]


def test_bot_commands_are_refused_in_groups(tgd):
    daemon, _, _ = tgd()
    assert "bot command" in tell(daemon, "telegram.send", chat="Friends", text="/ban everyone")["error"]


def test_always_button_grants_exactly_the_shown_chat(tgd):
    daemon, client, store = tgd()
    tell(daemon, "telegram.send", chat="Anna K", text="first")
    (item,) = pending(daemon)["items"]
    assert decide(daemon, item, "a")["status"] == "done"
    assert store.granted(ANNA.id, "send") and not store.granted(FRIENDS_ID, "send")
    assert tell(daemon, "telegram.send", chat="Anna K", text="second")["ok"]  # now without asking
    assert [t for _, t, _ in client.sent] == ["first", "second"]
    assert "until" in tell(daemon, "telegram.permissions")["text"]


def test_always_is_refused_where_it_was_not_offered(tgd):
    daemon, _, _ = tgd()
    tell(daemon, "telegram.send", pid=601, chat="Anna K", text="from a group run")
    (item,) = pending(daemon)["items"]
    reply = decide(daemon, item, "a")
    assert not reply["ok"] and "no “always” option" in reply["error"]
    assert daemon.approvals.get(item["id"])["status"] == "pending"  # a plain Approve/Reject still works


def test_deleting_always_asks_and_shows_what_goes(tgd):
    daemon, client, _ = tgd(rules={"chats": [{"chat": 11, "allow": ["send", "edit"]}]})
    assert tell(daemon, "telegram.delete", chat="Anna K", messages=[5]).get("approval")
    (item,) = pending(daemon)["items"]
    assert "DELETE 1 of your Telegram messages FOR EVERYONE" in item["summary"] and "hi Anna" in item["summary"]
    assert decide(daemon, item)["status"] == "done" and client.deleted == [(ANNA.id, [5])]
    assert "isn't yours" in tell(daemon, "telegram.delete", chat="Anna K", messages=[6])["error"]


def test_ampi_can_fix_its_own_fresh_message_but_the_owners_need_a_request(tgd):
    daemon, client, _ = tgd(rules=ALLOW_ANNA)
    sent = tell(daemon, "telegram.send", chat="Anna K", text="see you at 12")
    assert sent["ok"]
    mid = client.next_id
    assert tell(daemon, "telegram.edit", chat="Anna K", message=mid, text="see you at noon")["ok"]
    assert tell(daemon, "telegram.edit", chat="Anna K", message=5, text="hello Anna").get("approval")
    assert "48 hours" in tell(daemon, "telegram.edit", chat="Anna K", message=7, text="new")["error"]
    assert "same as the current" in tell(daemon, "telegram.edit", chat="Anna K", message=5, text="hi Anna")["error"]


def test_never_and_config_errors(tgd):
    daemon, _, _ = tgd(rules={"chats": [{"chat": 11, "never": True}]})
    assert "doesn't allow" in tell(daemon, "telegram.send", chat="Anna K", text="hi")["error"]
    with pytest.raises(ValueError, match="deleting always asks"):
        tg.TelegramRules.parse({"chats": [{"chat": 11, "allow": ["delete"]}]})
    with pytest.raises(ValueError):
        tg.TelegramRules.parse({"chats": [{"chat": "Anna K", "allow": ["send"]}]})
    daemon, _, _ = tgd(rules={"default": "never"})
    assert "doesn't allow" in tell(daemon, "telegram.send", chat="Anna K", text="hi")["error"]


def test_auto_approve_cant_list_telegram_writes(tmp_path):
    from ampi_tools.server import load_policy
    cfg = tmp_path / "config.toml"
    cfg.write_text('auto_approve = ["telegram.send"]\n')
    with pytest.raises(SystemExit, match="Telegram writes"):
        load_policy(cfg)


def test_duplicates_sizes_and_the_pending_cap(tgd):
    daemon, client, _ = tgd(rules=ALLOW_ANNA)
    assert tell(daemon, "telegram.send", chat="Anna K", text="on my way")["ok"]
    assert "last 10 minutes" in tell(daemon, "telegram.send", chat="Anna K", text="on my way")["error"]
    long_text = "word " * 700
    assert "at most 3000" in tell(daemon, "telegram.send", chat="Friends", text=long_text.strip())["error"]
    for i in range(3):
        assert tell(daemon, "telegram.send", chat="Friends", text=f"news number {'one two three'.split()[i]}")[
            "approval"]
    assert "3 Telegram requests are already waiting" in tell(daemon, "telegram.send", chat="Friends",
                                                              text="another one")["error"]


def test_an_unknown_outcome_says_so(tgd):
    daemon, client, store = tgd(rules=ALLOW_ANNA, unknown=True)
    reply = tell(daemon, "telegram.send", chat="Anna K", text="see you at noon")
    assert not reply["ok"] and "MAY have gone through" in reply["error"]
    assert "last 10 minutes" in tell(daemon, "telegram.send", chat="Anna K", text="see you at noon")["error"]


def test_revoking_only_from_the_private_chat(tgd):
    daemon, _, store = tgd()
    store.grant(ANNA.id, "send", "Anna K")
    assert "only be changed from the owner's private chat" in tell(daemon, "telegram.revoke", pid=601,
                                                                   chat="Anna K")["error"]
    assert "Removed 1" in tell(daemon, "telegram.revoke", chat="Anna K")["text"]
    assert not store.granted(ANNA.id, "send")


def test_requests_before_the_protect_list_are_refused(tmp_path):
    store = tg.TelegramStore(tmp_path / "t.sqlite3")
    with pytest.raises(ToolError, match="isn't available"):
        store.protected()
    store.set_protected([1], complete=False)
    with pytest.raises(ToolError, match="first check"):
        store.protected()


def test_telegram_requests_expire_after_a_day_and_stuck_ones_are_reported(tmp_path):
    approvals = Approvals(tmp_path / "a.sqlite3")
    a = approvals.add([{"op": "telegram.send", "args": {}}], "s", None)
    b = approvals.add([{"op": "todoist.delete", "args": {}}], "s", None)
    approvals.db.execute("UPDATE approvals SET created=?", (time.time() - 2 * 86400,))
    approvals.db.commit()
    approvals.expire()
    assert approvals.get(a)["status"] == "expired" and approvals.get(b)["status"] == "pending"
    approvals.claim(b, "approved")
    assert approvals.recover() == [b] and approvals.get(b)["status"] == "unknown"


@pytest.mark.parametrize("text,ok", [
    ("Hello, Anna! 😀", True), ("family 👨‍👩‍👧 and 1️⃣ ❤️", True), ("line one\nline two", True), ("line one\n  indented", False), ("two  spaces", False), ("ok\U000e0100\U000e0101", False), ("ok\ufe00", False), ("ok\u180b", False),
    ("zero​width", False), ("bidi‮trick", False), ("nbsp here", False), ("tab\there", False),
    ("café", False), ("", False),
])
def test_message_text_check(text, ok):
    if ok:
        assert tg.check_message(text) == text
    else:
        with pytest.raises(ToolError):
            tg.check_message(text)


@pytest.mark.parametrize("text,safe", [
    ("see you at noon", True), ("Okay, sounds good!", True), ("go to example.com", False),
    ("https://x.y", False), ("t.me/joinchat", False), ("call +44 20", False), ("1234", False),
    ("/start", False), ("@anna", False), ("Grеat", False),
])
def test_auto_safe_text(text, safe):
    assert tg.auto_safe(text) is safe


# --- the client ----------------------------------------------------------------------------------------

def test_drafts_must_be_fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("draft-1.txt").write_text("hello\n")
    args = cli.telegram_parser().parse_args(["send", "Anna K", "-f", "draft-1.txt"])
    assert cli.telegram_ops(args)[0]["args"] == {"chat": "Anna K", "text": "hello"}
    old = time.time() - 3600
    os.utime("draft-1.txt", (old, old))
    with pytest.raises(SystemExit, match="minutes ago"):
        cli.telegram_ops(args)


def test_cli_shapes():
    p = cli.telegram_parser()
    assert cli.telegram_ops(p.parse_args(["delete", "Anna K", "5", "6"]))[0] == {
        "op": "telegram.delete", "args": {"chat": "Anna K", "messages": [5, 6]}}
    assert cli.telegram_ops(p.parse_args(["send", "me", "note", "--reply-to", "7"]))[0]["args"] == {
        "chat": "me", "text": "note", "reply_to_msg": 7}


# --- the bridge -----------------------------------------------------------------------------------------

def test_protected_chats_cover_bots_groups_and_watched_chats():
    from assistant_bridge.main import protected_chats

    class Store:
        def __init__(self, state):
            self.state = state

        def get_watch_state(self, profile, name):
            return self.state

    profile = NS(bot_token="12345:abc", groups=[NS(chat_id=-1001)],
                 watchers=[NS(kind="telegram", name="clip"), NS(kind="gmail", name="g")], name="main")
    config = NS(profiles=[profile])
    assert protected_chats(config, Store({"peer_id": -1002})) == ([-1002, -1001, 12345], True)
    assert protected_chats(config, Store(None)) == ([-1001, 12345], False)


def test_relay_shows_the_always_button_on_its_own_row_and_parses_answers():
    from assistant_bridge.approvals import buttons_for, request_text
    item = {"id": 3, "nonce": "ab", "summary": "s", "grant_label": "send in “Anna K” without asking for 30 days"}
    rows = buttons_for(item)
    assert [b["callback_data"] for b in rows[0]] == ["ap:3:ab:y", "ap:3:ab:n"] and rows[1][0]["callback_data"] == \
        "ap:3:ab:a"
    assert "also allows: send in “Anna K”" in request_text(item)
    assert len(buttons_for({"id": 3, "nonce": "ab", "summary": "s"})) == 1


# --- review round 1 ----------------------------------------------------------------------------------

def test_one_telegram_write_per_request(tgd):
    daemon, client, _ = tgd(rules=ALLOW_ANNA)
    ops = [{"op": "telegram.send", "args": {"chat": "Anna K", "text": f"hello {w}"}} for w in ("one", "two")]
    reply = run(daemon.handle({"cmd": "run", "ops": ops}, 501))
    assert "one Telegram write per request" in reply["error"] and client.sent == []


def test_errors_that_may_have_reached_telegram_are_unknown_not_failed(tmp_path):
    writer, client = make_writer(tmp_path)
    client.fail = ConnectionError("reset")
    reply = w(writer, cmd="send", peer_id=ANNA.id, text="x")
    assert reply.get("unknown") and not reply.get("not_sent")
    refused = type("ChatWriteForbiddenError", (Exception,), {})
    client.fail = refused("no")
    reply = w(writer, cmd="send", peer_id=ANNA.id, text="y")
    assert reply.get("not_sent") and "can't write" in reply["error"]


def test_refused_requests_dont_use_the_write_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("tg_reader.writer.WRITES_PER_MINUTE", 1)
    writer, client = make_writer(tmp_path)
    assert "isn't yours" in w(writer, cmd="edit", peer_id=ANNA.id, message_id=6, text="x", expect={})["error"]
    assert w(writer, cmd="send", peer_id=ANNA.id, text="still allowed")["ok"]


def test_strangers_never_get_automatic_messages_or_always(tgd, monkeypatch):
    monkeypatch.setattr(ANNA, "contact", False)
    daemon, client, _ = tgd(rules=ALLOW_ANNA)
    assert tell(daemon, "telegram.send", chat="Anna K", text="see you at noon").get("approval")
    (item,) = pending(daemon)["items"]
    assert "NOT in your contacts" in item["summary"] and "grant_label" not in item


def test_chat_titles_cant_fake_the_approval_text():
    line = tg.chat_line({"kind": "group", "peer_id": -100123, "title": 'Mom" (person, id 1) “x”', "members": 3000})
    assert line.startswith("[group id -100123, 3000 members] named:") and '"' not in line and "(" not in line


def test_never_beats_require_approval(tgd):
    daemon, _, _ = tgd(rules={"default": "never"}, policy=Policy(require_approval=frozenset({"telegram.send"})))
    assert "doesn't allow" in tell(daemon, "telegram.send", chat="Anna K", text="hi")["error"]


def test_pending_requests_count_for_the_duplicate_guard(tgd):
    daemon, _, _ = tgd()
    assert tell(daemon, "telegram.send", chat="Anna K", text="on my way").get("approval")
    assert "last 10 minutes" in tell(daemon, "telegram.send", chat="Anna K", text="on my way")["error"]


@pytest.mark.parametrize("text", ["12 345", "1.234.567", "see сайт.рф", "code 12-34-56"])
def test_more_risky_text_asks(text):
    assert not tg.auto_safe(text)


def test_notices_are_acknowledged_only_after_they_were_sent():
    from assistant_bridge.approvals import ApprovalRelay
    from assistant_bridge.telegram import TelegramError

    class Api:
        def __init__(self):
            self.fail, self.sent = True, []

        async def send_message(self, chat_id, text, thread_id=None, parse_mode=None):
            if self.fail:
                raise TelegramError("sendMessage", "down")
            self.sent.append(text)

    acks = []

    async def ask(path, payload, timeout=300):
        acks.append(payload)
        return {"ok": True}

    bot = NS(api=Api(), profile=NS(owner_chat_id=1))
    relay = ApprovalRelay(bot, "sock", ask)
    run(relay.send_notices([{"id": 7, "text": "Sent as you"}]))
    assert acks == []
    bot.api.fail = False
    run(relay.send_notices([{"id": 7, "text": "Sent as you"}]))
    assert acks == [{"cmd": "notices.ack", "id": 7}] and bot.api.sent == ["Sent as you"]


def test_runs_are_registered_for_every_claude_process(tmp_path, fake_claude_env):
    from conftest import FAKE_CLAUDE
    from assistant_bridge.claude_runner import ClaudeRunner, clean_env
    runner = ClaudeRunner(FAKE_CLAUDE, timeout_seconds=30, max_concurrent=1, env=clean_env(os.environ, []))
    seen = []

    async def spawn(pid, origin):
        seen.append(("spawn", origin))

    async def gone(pid):
        seen.append(("exit",))

    runner.on_spawn, runner.on_exit = spawn, gone
    run(runner.run_turn("hi", cwd=tmp_path, session_id="00000000-0000-0000-0000-000000000002", started=False,
                        model=None, allowed_tools=["Read"], origin="group:x"))
    assert seen[0] == ("spawn", "group:x") and seen[-1] == ("exit",)


# --- review round 2 ----------------------------------------------------------------------------------

def test_a_resent_request_is_an_unknown_outcome(tmp_path):
    writer, client = make_writer(tmp_path)
    client.fail = type("RandomIdDuplicateError", (Exception,), {"code": 400})("dup")
    assert w(writer, cmd="send", peer_id=ANNA.id, text="x").get("unknown")


def test_member_count_failures_are_cached_and_cost_budget(tmp_path):
    writer, client = make_writer(tmp_path)
    calls = []

    async def full(request):
        calls.append(request)
        raise FloodWaitError(30)

    client.__call__ = full
    type(client).__call__ = lambda self, req: full(req)
    group = NS(id=77, title="Club", participants_count=None, megagroup=True, bot=False)
    assert run(writer._member_count(-100077, group)) is None
    assert writer.reader._cooldown_until > writer.reader.clock()
    assert run(writer._member_count(-100077, group)) is None
    assert len(calls) <= 1  # the failure is remembered (and telethon may be missing here: then 0 calls)


def test_a_busy_automatic_write_sends_nothing(tgd):
    daemon, client, _ = tgd(rules=ALLOW_ANNA)

    async def scenario():
        await daemon._tg_lock.acquire()
        try:
            import ampi_tools.server as srv
            orig = srv.asyncio.wait_for

            async def quick(aw, timeout):
                return await orig(aw, 0.05)
            srv.asyncio.wait_for = quick
            try:
                return await daemon.handle({"cmd": "run", "ops": [{"op": "telegram.send", "args": {
                    "chat": "Anna K", "text": "see you at noon"}}]}, 501)
            finally:
                srv.asyncio.wait_for = orig
        finally:
            daemon._tg_lock.release()

    reply = run(scenario())
    assert "nothing was sent" in reply["error"] and client.sent == []
