"""Read-only access to the owner's own Telegram account (chats, groups, channels).

The Bot API can't read history or chats the bot isn't in, so this logs in as the owner with
Telethon (Telegram's client protocol). The login session can do anything the owner can, so it
never leaves the separate `tgreader` Linux user: a small daemon (server.py) holds it and answers
a fixed set of read-only requests over a Unix socket. The assistant only gets the client
(client.py, the `tg-read` command), which can't send, edit or delete anything.
"""
