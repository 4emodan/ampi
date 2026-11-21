# Claude Assistant — project context

A personal AI assistant, similar in spirit to OpenClaw, built on **official Claude Code** so it runs on the owner's **Claude subscription** (not pay-per-use API billing).

Requirements live in [REQUIREMENTS.md](REQUIREMENTS.md). Treat them as the source of truth; ask before deviating.

## Hard constraints (decided)

- **Billing:** must run on the Claude subscription. Use only the unmodified `claude` CLI signed in with the owner's Claude account.
  - Third-party harnesses (OpenClaw etc.) are not covered by the subscription since 2026-04-04.
  - Never use `claude --bare` — it ignores the subscription login and needs an API key.
  - No Agent SDK packages with API keys, no proxies that reuse subscription tokens.
- **Host:** existing Hetzner VPS — 2 vCPU, 4 GB RAM, 40 GB disk, Linux, **shared with other projects**.
  - No local models (no GPU). The model runs at Anthropic.
  - Run as its own Linux user; must not be able to read or touch the other projects.
- **Interface building blocks available:**
  - Claude Code **channels** (`--channels plugin:telegram@claude-plugins-official`) — research preview, session must stay running.
  - `claude -p` called from own scripts (e.g. a Telegram bot, cron jobs); `--resume <session-id>` for continuity.
  - Remote Control from the Claude mobile app.
- **Security:** only the owner may talk to it (sender allowlist / Telegram user ID check). Start integrations read-only; least-privilege `--allowedTools` / `permissions.allow`. No `--dangerously-skip-permissions` on the shared server.

## Decisions (2026-11-15)

- **Only the owner uses it.** Family members may not use the owner's Claude subscription (Consumer Terms: may not "make your Account available to anyone else"). "Multiple users" = multiple **profiles of the owner** (e.g. personal, work), each with its own workspace, memory, persona, limits and integrations, all on the owner's subscription. If family is added later, each needs their own Claude plan or an API key.
- **Messenger: Telegram, via the stock Telegram apps** (2026-11-16). WhatsApp dropped as not worth the cost/hassle. No custom Telegram client for now; a companion app may come later for phone-only features (alarms, geofencing, calls).
  - **Profile = one Telegram bot** (one BotFather bot per profile; free).
  - **Conversation = a topic in the private chat with that bot** (Bot API 9.3+ private-chat topics; bots can `createForumTopic` there since 9.4). Key sessions by (bot, `message_thread_id`).
  - Stream replies with `sendMessageDraft`; approvals via inline keyboard buttons.
  - File limits on the cloud Bot API: ~20 MB download / 50 MB upload; self-hosted Bot API server raises this if needed.
- **Custom bridge, not the channels plugin:** multiple profiles × multiple long-running conversations each → bridge maps (profile, conversation) → `claude -p --resume <session-id>`.
- **Approval for send/delete** goes through our own tool that asks the owner in chat (✅/❌) before acting.

## Layout

```
src/assistant_bridge/   the bridge (Python 3.11+, only dependency httpx)
  config.py             config.toml loading/validation
  telegram.py           minimal Bot API client (raw HTTP, no wrapper library)
  store.py              SQLite: conversations (topic -> session), run log, update offsets
  claude_runner.py      runs `claude -p --output-format json` (--session-id / --resume), strips API-key env vars
  text.py               Markdown -> Telegram HTML, message splitting
  bot.py                one ProfileBot per Telegram bot: routing, commands, attachments, group batches
  approvals.py          shows ampi-tools approval requests as Telegram buttons, sends back the answer
  watchers.py           checks Gmail/Todoist/Telegram/the clock and starts Claude runs (watchers/<name>.md)
  services.py           async client for the helper daemons' Unix sockets
  chatlog.py            logs every conversation to <workspace>/chats/ so any session can read the others
                        (the Bot API can't fetch chat history; these logs are the only history)
  main.py               entry point (`claude-assistant --config ... [--check]`)
src/tg_reader/          optional read-only access to the owner's Telegram account (Telethon)
  server.py             `tg-reader serve|login`: daemon as user `tgreader`, holds the session, Unix socket
  reader.py             the only allowed requests: list chats, read a chat, search (never send/delete)
  client.py             `tg-read`: stdlib-only client the assistant runs (skill: templates/skills/telegram)
src/ampi_tools/         optional Todoist, Gmail, YouTube, Threads, Instagram with owner approval for critical actions
  server.py             `ampi-tools serve|gmail-login|youtube-login|check`: daemon as user `ampitools`, holds the tokens
  ops.py                every operation, which ones are critical, the approvals queue
  todoist.py, gmail.py  API clients (httpx; Todoist API v1, Gmail REST with gmail.modify)
  youtube.py            YouTube Data API v3 + public RSS/oEmbed (no transcripts: blocked from the server);
                        rate limiter and daily quota budgets
  meta.py, ops_social.py Threads + Instagram (Meta's official APIs: own posts and replies/comments only);
                        token refresh, secret redaction, checks on published text
  maps.py, ops_maps.py  Google Maps via Google's agent API (mapstools.googleapis.com, MAPS_API_KEY): places,
                        routes, weather, link resolving; local daily/monthly budget; answers carry source links
  wolt.py, ops_wolt.py  Wolt via wolt.com's own (unofficial) web API, owner's choice: venues, menus, orders,
                        filling the basket; never places/pays an order; polite limiter, pause on any block
  vault.py              the owner's profile + card (vault.json 0600, entered with `ampi-tools vault-set`), masking,
                        per-site grants and the purchase log (vault.sqlite3)
  ops_checkout.py       info.* / checkout.* / payment.*: fill by reference after approval, card only into payment
                        provider frames, daemon presses Pay; private chat only; [payment] limits
  checkout.py           stdlib; the checkout controller (user `ampicheckoutctl`) driving `obscura serve` (user
                        `ampicheckout`) over CDP; elements tracked in isolated worlds; never returns input values
  checkout_net.py       root: nft table for the engine's egress and the loopback control port
  client.py             `todoist` / `gmail` / `youtube` / `threads` / `instagram` / `maps` / `wolt` / `browse` /
                        `info` / `checkout` / `payment` commands the assistant runs (skills: templates/skills/)
  browser.py            stdlib-only; one sandboxed `ampi-browser@` instance per page (socket-activated, user
                        `ampibrowser`) running the Obscura engine read-only; not part of the ampi-tools daemon
  browser_net.py        root: nft egress table for the browser user + generated resolv.conf/hosts/passwd
tests/                  pytest; fake claude CLI + fake Telegram API, no network
deploy/                 install.sh, systemd unit (runs as user `assistant`, isolated);
                        install-tg-reader.sh, tg-reader.service (user `tgreader`);
                        install-tools.sh, ampi-tools.service (user `ampitools`);
                        install-browser.sh, ampi-browser.socket + ampi-browser@.service + ampi-browser-net.service
templates/               starter CLAUDE.md, group/watcher instructions, skills
docs/SETUP.md           server setup steps
```

Run tests with `uv run pytest`.
