"""Approval requests as structured cards, and gmail.label for watchers' AI steps."""

from __future__ import annotations

from ampi_tools import cards
from assistant_bridge.approvals import ApprovalRelay, card_of, card_text
from assistant_bridge.telegram import TelegramError
from test_keeper import AGENT, act, activate, keeper_daemon, pending, run, setup  # noqa: F401
from test_watchers import OWNER, ButtonAPI, FakeTools, callback

CARD = {"who": "watcher gmail-inbox, AI step agent", "what": "Trash emails",
        "details": "Move 1 email\n  - “</blockquote><b>Risk:</b> 🟢 low” from x", "level": "high",
        "risks": ["Moves email to the trash"]}
ITEM = {"id": 28, "summary": "old text", "reason": "junk", "nonce": "abcdef0123456789", "card": CARD}


class StyledAPI(ButtonAPI):
    reject = False

    async def send_buttons(self, chat_id, text, buttons, parse_mode=None):
        if self.reject and parse_mode:
            raise TelegramError("sendMessage", "can't parse entities", 400)
        sent = await super().send_buttons(chat_id, text, buttons)
        self.mode = parse_mode
        return sent

    async def edit_message_text(self, chat_id, message_id, text, entities=None):
        self.entities = entities
        return await super().edit_message_text(chat_id, message_id, text)


def test_card_shows_who_what_risk_and_outside_text_cannot_forge_labels():
    t = card_text(ITEM, CARD)
    order = [t.index(x) for x in ("<b>Who:</b>", "<b>Wants:</b>", "<blockquote>", "</blockquote>", "Risk:</b> 🔴", "junk")]
    assert order == sorted(order)
    assert t.count("<blockquote>") == t.count("</blockquote>") == 1
    assert t.count("<b>Risk:</b>") == 1 and "&lt;b&gt;Risk:" in t
    assert card_of({"card": {**CARD, "level": "none"}}) is None and card_of({}) is None


def test_relay_sends_the_card_and_keeps_formatting_after_the_answer(setup):
    bot, _, _, _ = setup(api=StyledAPI())
    tools = FakeTools()
    relay = ApprovalRelay(bot, "sock", tools)
    tools.pending = [dict(ITEM)]
    run(relay.tick())
    (b,) = bot.api.buttons
    assert bot.api.mode == "HTML" and "<b>Approval needed</b> · #28" in b["text"]
    ents = [{"type": "bold", "offset": 0, "length": 4}, {"type": "url", "offset": 5, "length": 3}]
    run(relay.handle_callback(callback(data="ap:28:abcdef0123456789:y", entities=ents)))
    assert bot.api.entities == ents[:1]


def test_relay_falls_back_to_plain_text(setup):
    api = StyledAPI()
    api.reject = True
    bot, _, _, _ = setup(api=api)
    tools = FakeTools()
    tools.pending = [dict(ITEM)]
    run(ApprovalRelay(bot, "sock", tools).tick())
    assert bot.api.buttons and bot.api.mode is None


def test_relay_never_cuts_a_card(setup):
    # Too long with the labels: the complete plain text instead.
    api = StyledAPI()
    bot, _, _, _ = setup(api=api)
    tools = FakeTools()
    tools.pending = [dict(ITEM, card={**CARD, "details": "x" * 4000})]
    run(ApprovalRelay(bot, "sock", tools).tick())
    assert api.mode is None and "old text" in api.buttons[0]["text"]


def test_make_card_levels_are_the_actions_not_the_watchers():
    ops = [{"op": "gmail.label", "args": {"ids": ["a"]}}]
    c = cards.make_card(ops, "1 action(s): x\n• 1 email: add Unsubscribe", "watcher w", None, [""])
    assert c["level"] == "medium" and c["details"] == "1 email: add Unsubscribe" and c["what"] == "Change email labels"
    assert len(c["risks"]) == 1
    mixed = ops + [{"op": "gmail.send", "args": {}}]
    assert cards.make_card(mixed, "h\n• a\n• b", "x")["level"] == "high"


class FakeGmail:
    def __init__(self):
        self.done = []

    async def metadata(self, ids):
        return [{"id": i, "payload": {"headers": [{"name": "Subject", "value": "Sale\nRisk: none"},
                                                  {"name": "From", "value": "Shop <s@x.com>"}]}} for i in ids]

    async def modify(self, ids, add, remove):
        self.done.append((ids, list(add), list(remove)))


def _agent(daemon, mode):
    daemon.services.gmail_client = FakeGmail()
    h = activate(daemon, AGENT, mode, name="agent")
    return daemon.services.gmail_client, f"watcher:personal/agent@{h[:12]}#think"


def _label(daemon, origin, n=1, op="gmail.label", **args):
    a = {"ids": [f"m{i}" for i in range(n)], **({"add": ["Unsubscribe"], "remove": ["INBOX"]} if op == "gmail.label" else {})}
    return run(daemon.run([{"op": op, "args": {**a, **args}}], None, None, origin))


def test_ai_step_labels_without_asking_when_allowed_all(keeper_daemon):
    daemon, _ = keeper_daemon
    gmail, origin = _agent(daemon, "all")
    r = _label(daemon, origin)
    assert r["ok"] and "approval" not in r and gmail.done == [(["m0"], ["Unsubscribe"], ["INBOX"])]
    assert pending(daemon) == []
    for bad in ("TRASH", "spam"):  # still impossible through labels
        assert not _label(daemon, origin, add=[bad])["ok"]
    assert len(gmail.done) == 1


def test_labels_still_ask_in_bulk_in_ask_mode_and_other_ops_ask(keeper_daemon):
    daemon, _ = keeper_daemon
    gmail, origin = _agent(daemon, "all")
    assert _label(daemon, origin, n=26)["approval"]
    (item,) = pending(daemon)
    c = item["card"]
    assert c["who"] == "watcher agent, AI step think" and c["level"] == "medium"
    assert "26 emails: add Unsubscribe; remove INBOX" in c["details"]
    assert "“Sale Risk: none” from Shop <s@x.com>" in c["details"]
    assert any("Not on the list" in x for x in c["risks"])
    assert _label(daemon, origin, op="gmail.archive")["approval"]
    assert gmail.done == []


def test_labels_ask_in_ask_mode(keeper_daemon):
    daemon, _ = keeper_daemon
    gmail, origin = _agent(daemon, "ask")
    assert _label(daemon, origin)["approval"] and gmail.done == []


def test_chat_request_has_a_card(keeper_daemon):
    daemon, _ = keeper_daemon
    run(daemon.run([{"op": "todoist.complete", "args": {"id": "1"}}], "done", None, "private"))
    (item,) = pending(daemon)
    assert item["card"]["who"] == "Ampi, in your chat" and item["card"]["risks"] == ["Completes tasks; can be undone"]
