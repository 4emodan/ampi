import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from tg_reader import client
from tg_reader.reader import Reader, format_message, parse_when

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
ANNA = NS(id=11, first_name="Anna", last_name="K", username="anna_k", bot=False)
CLIP = NS(id=9876543210, title="Clipboard", username=None)
NEWS = NS(id=300, title="News Channel", username="newsch")
CLIP2 = NS(id=400, title="Clipboard old", username=None)


def dialog(entity, name, *, is_user=False, is_group=False, is_channel=False, unread=0, days=0):
    return NS(entity=entity, name=name, id=-1000000000000 - entity.id if not is_user else entity.id,
              is_user=is_user, is_group=is_group, is_channel=is_channel, unread_count=unread,
              date=T0 + timedelta(days=days))


def message(i, text="", minutes=0, sender=ANNA, **extra):
    base = dict(id=i, date=T0 + timedelta(minutes=minutes), message=text, sender=sender, out=False,
                action=None, media=None, forward=None, reply_to_msg_id=None, entities=None, chat=CLIP)
    base.update(extra)
    return NS(**base)


class FakeClient:
    def __init__(self, dialogs, messages):
        self.dialogs = dialogs
        self.messages = messages  # entity id -> list oldest first
        self.calls = []

    async def iter_dialogs(self, limit=None):
        for d in self.dialogs[:limit]:
            yield d

    async def iter_messages(self, entity, limit=None, offset_date=None, search=None, min_id=0):
        self.calls.append(dict(entity=entity, limit=limit, offset_date=offset_date, search=search))
        pool = [m for msgs in self.messages.values() for m in msgs] if entity is None else \
            list(self.messages.get(getattr(entity, "id", entity), []))
        count = 0
        for m in sorted(pool, key=lambda m: m.date, reverse=True):  # newest first, like Telegram
            if m.id <= min_id:
                continue
            if offset_date and m.date >= offset_date:
                continue
            if search and search.lower() not in (m.message or "").lower():
                continue
            if limit is not None and count >= limit:
                return
            count += 1
            yield m


@pytest.fixture
def reader():
    dialogs = [
        dialog(CLIP, "Clipboard", is_group=True, is_channel=True, unread=3, days=5),
        dialog(ANNA, "Anna K", is_user=True, days=4),
        dialog(NEWS, "News Channel", is_channel=True, days=3),
        dialog(CLIP2, "Clipboard old", is_group=True, days=1),
    ]
    msgs = {CLIP.id: [message(i, f"item {i}", minutes=i * 60) for i in range(1, 6)],
            ANNA.id: [message(50, "the flat is ready", minutes=10), message(51, "ok", minutes=20, out=True)]}
    return Reader(FakeClient(dialogs, msgs))


def run(coro):
    return asyncio.run(coro)


def test_chats_lists_kinds_and_filters(reader):
    text = run(reader.chats())
    assert "- Clipboard · group · id" in text and "3 unread" in text
    assert "Anna K · person · id 11 · @anna_k" in text
    assert "News Channel · channel" in text
    assert run(reader.chats(query="news")).count("\n") == 0


def test_resolve_by_exact_name_beats_partial(reader):
    entity, name = run(reader.resolve("clipboard"))
    assert entity is CLIP


def test_resolve_ambiguous_lists_ids(reader):
    res = run(reader.handle({"cmd": "history", "chat": "clip"}))
    assert not res["ok"] and "2 chats match" in res["error"] and "Clipboard old" in res["error"]


def test_resolve_by_id_and_username(reader):
    assert run(reader.resolve("11"))[0] is ANNA
    assert run(reader.resolve("@newsch"))[0] is NEWS
    assert run(reader.resolve("me"))[0] == "me"


def test_history_newest_n_printed_oldest_first(reader):
    text = run(reader.history("Clipboard", limit=3))
    assert text.index("item 3") < text.index("item 4") < text.index("item 5")
    assert "item 2" not in text
    assert "limit 3 reached" in text
    assert text.startswith("# Clipboard — 3 messages, 2026-11-01 15:00 to 2026-11-01 17:00 UTC")


def test_history_range_and_search(reader):
    text = run(reader.history("Clipboard", since="2026-11-01T14:00", until="2026-11-01T16:30"))
    assert [n for n in range(1, 6) if f"item {n}" in text] == [2, 3, 4]
    assert "limit" not in text
    text = run(reader.history("Anna", search="flat"))
    assert "the flat is ready" in text and "ok" not in text.split("\n", 1)[1]


def test_until_bare_date_includes_that_day():
    assert parse_when("2026-11-01", end_of_day=True) == datetime(2026, 11, 2, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        parse_when("yesterday")


def test_global_search_names_chat(reader):
    text = run(reader.search("flat"))
    assert "· Anna K (#50) in Clipboard" in text


def test_format_message_details():
    m = message(7, "see this", out=True, reply_to_msg_id=5,
                forward=NS(chat=NS(title="Tech news"), sender=None),
                entities=[NS(url="https://example.com/a")], photo=True)
    text = format_message(m)
    assert text.splitlines()[0] == "### 2026-11-01 12:00 UTC · Me (#7)"
    assert "(forwarded from Tech news; reply to #5)" in text
    assert "[photo]" in text and "[links: https://example.com/a]" in text


def test_errors_are_returned_not_raised(reader):
    class Boom(Exception):
        pass

    async def broken(*a, **k):
        raise Boom("flood wait 30s")
        yield  # pragma: no cover

    reader.client.iter_messages = broken
    res = run(reader.handle({"cmd": "history", "chat": "Anna"}))
    assert res == {"ok": False, "error": "Boom: flood wait 30s"}
    assert run(reader.handle({"cmd": "send", "chat": "Anna"}))["ok"] is False


def test_client_saves_long_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert client.deliver("short", None, "x") == "short"
    long = "# Clipboard — 900 messages\n" + "x" * 30_000
    out = client.deliver(long, None, "Clipboard", now=T0)
    saved = tmp_path / "telegram" / "20261101-120000-clipboard.md"
    assert saved.read_text(encoding="utf-8").startswith("# Clipboard")
    assert "Saved" in out and "telegram" in out


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_socket_round_trip(reader, tmp_path):
    from tg_reader.server import serve_socket

    path = str(tmp_path / "s.sock")

    async def go():
        server = await serve_socket(reader, path)
        async with server:
            return await asyncio.to_thread(client.request, {"cmd": "history", "chat": "Anna"}, path, 5)

    res = run(go())
    assert res["ok"] and "the flat is ready" in res["text"]


def test_client_reports_missing_daemon(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix sockets")
    res = client.request({"cmd": "chats"}, str(tmp_path / "none.sock"))
    assert res["ok"] is False and "isn't running" in res["error"]


def test_bridge_prompt_mentions_skill_when_installed(tmp_path):
    from assistant_bridge.bot import TELEGRAM_SKILL, _conversations_prompt

    profile = NS(workspace=tmp_path, groups=(), watchers=())
    assert "Telegram doesn't let you fetch" in _conversations_prompt(profile)
    (tmp_path / TELEGRAM_SKILL).parent.mkdir(parents=True)
    (tmp_path / TELEGRAM_SKILL).write_text("x")
    assert "telegram skill" in _conversations_prompt(profile)


def test_json_payload_shape():
    args = client.build_parser().parse_args(["history", "Clipboard", "-n", "5"])
    payload = {k: v for k, v in vars(args).items() if k != "out" and v is not None}
    assert json.loads(json.dumps(payload)) == {"cmd": "history", "chat": "Clipboard", "limit": 5}


def test_since_reports_only_newer_messages(reader):
    base = run(reader.handle({"cmd": "since", "chat": "Clipboard"}))
    assert base == {"ok": True, "text": "", "count": 0, "last_id": 5, "chat": "Clipboard"}
    new = run(reader.handle({"cmd": "since", "chat": "Clipboard", "after_id": 3}))
    assert new["count"] == 2 and new["last_id"] == 5
    assert new["text"].index("item 4") < new["text"].index("item 5") and "item 3" not in new["text"]
