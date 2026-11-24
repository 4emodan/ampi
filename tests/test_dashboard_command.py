"""/dashboard: the owner's private chat only, and the link never reaches the chat logs."""

from test_bot import CHAT, OWNER, msg, run, setup  # noqa: F401 (fixture)

from assistant_bridge.info_ui import InfoUI


def test_link_only_in_the_private_chat(setup, tmp_path):  # noqa: F811
    bot, api, runner, _ = setup()
    asked, sent = [], []

    async def ask(socket, req, timeout=None):
        asked.append(req["cmd"])
        return {"ok": True, "url": "https://192.0.2.1/ampi/#k=SECRET"}

    async def send(chat_id, text, thread_id=None, parse_mode=None):
        sent.append((chat_id, text))
        return {"message_id": 1}

    api.send_message = send
    bot.info_ui = InfoUI(bot, "x", ask)
    run(bot.handle_message(msg("/dashboard")))
    assert asked == ["web.link"] and any("SECRET" in t for _, t in sent)
    run(bot.handle_message(msg("/dashboard off")))
    assert asked[-1] == "web.out"
    assert not runner.calls
    for p in bot.profile.workspace.rglob("*"):
        assert not p.is_file() or "SECRET" not in p.read_text(encoding="utf-8", errors="ignore")
