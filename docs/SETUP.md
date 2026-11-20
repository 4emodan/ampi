# Setup

How to get the MVP running on the Hetzner server. About 20 minutes.

## 1. Create the Telegram bot(s)

For each profile (e.g. "personal", later "work"):

1. In Telegram, open **@BotFather** → `/newbot` → pick a name and a username ending in `bot`. Copy the token.
2. Still in @BotFather, open the bot's settings and **turn on topics (threaded mode) for private chats**. That's what lets you keep several conversations side by side; without it `/new` just resets the current chat.

Find your own Telegram user ID: message **@userinfobot** (or start the bridge and send your bot a message; it logs the ID of anyone it ignores).

## 2. Install on the server

```bash
git clone <this repo> ~/claude-assistant   # or copy the folder with scp/rsync
cd ~/claude-assistant
sudo bash deploy/install.sh
```

The script creates a separate `assistant` Linux user, installs the app into `/home/assistant/app`, creates a config and env file if they don't exist, and installs the systemd service. It's safe to re-run for updates.

## 3. Sign in to Claude Code (once)

```bash
sudo -iu assistant
curl -fsSL https://claude.ai/install.sh | bash
claude
```

Open the login link it prints, sign in with your Claude account, then type `/exit`. This login is what makes the assistant use your subscription. Don't set `ANTHROPIC_API_KEY` for this user; the bridge strips it anyway.

## 4. Configure

All three are root-owned, so the assistant can't read the tokens or change its own config or persona. Edit them as root.

- `/etc/claude-assistant/env`: one line per bot, e.g. `TG_TOKEN_PERSONAL=123:ABC…`
- `/home/assistant/config.toml`: set `owner_ids` to your Telegram user ID; add a `[[profiles]]` block per extra bot.
- `/home/assistant/profiles/<name>/CLAUDE.md`: the profile's persona and rules (a template is created for `personal`).

Check it:

```bash
sudo bash /home/assistant/app/deploy/check.sh
```

## 5. Start

```bash
sudo systemctl start claude-assistant
journalctl -u claude-assistant -f
```

Send your bot a message. Commands: `/new [title]`, `/reset`, `/cancel`, `/model [name|default]`, `/status`, `/help`.

## 6. Groups (optional)

The bot can act on every message you post or forward in a group, e.g. a "Clipboard" group where you collect interesting things.

1. In @BotFather: `/setprivacy` → pick the bot → **Disable**. Without this, bots in groups only see commands and mentions.
2. Add the bot to the group (if it was already there, remove and re-add it so the setting applies).
3. Post something in the group. The log shows `message in a group that is not in the config: chat_id=-100…`.
4. Add the group to the profile in `config.toml`:
   ```toml
   [[profiles.groups]]
   chat_id = -100…
   name = "clipboard"
   ```
5. Write what to do with each item in `/home/assistant/profiles/<profile>/groups/clipboard.md` (a starter is in `templates/groups/clipboard.md`). You can also ask the assistant in the group to change it.
6. `sudo systemctl restart claude-assistant`.

Only your own messages are handled; anyone else in the group is ignored. Messages forwarded together are handled in one run (`batch_seconds`, default 5). If there's nothing worth saying, the bot stays silent.

## 7. Reading all your Telegram chats (optional)

A bot can't read history or chats it isn't in. The `tg-reader` service logs in to Telegram **as you** (with Telethon) and gives the assistant a read-only `tg-read` command plus a `telegram` skill, so you can ask "read the last 100 messages in Clipboard" or "what did Anna say about the flat".

The login session could do anything your account can, so it is kept away from the assistant: it lives with a separate `tgreader` user whose home the assistant can't see, and the assistant can only send it fixed read-only requests (list chats, read a chat, search) over `/run/tg-reader/tg-reader.sock`. Nothing it does marks messages as read.

1. `sudo bash deploy/install-tg-reader.sh`
2. At <https://my.telegram.org> → **API development tools**, create an app (any name) and put its `api_id` and `api_hash` in `/home/tgreader/env`.
3. Log in; Telegram sends a code to your app, and you type it (and your 2FA password, if set) yourself:
   ```bash
   sudo -u tgreader -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/tg-reader login'
   ```
   If no code arrives (Telegram sometimes doesn't send codes to a new API app), use `login-qr` instead: it prints a QR code that you scan with your phone under Telegram → Settings → Devices → **Link Desktop Device**. Make the terminal wide enough to show the whole code.
4. `systemctl restart tg-reader && journalctl -u tg-reader -n 20` (should say "listening").
5. In `config.toml`, add `"Skill"` and `"Bash(tg-read:*)"` to `allowed_tools`, then `systemctl restart claude-assistant`.

To revoke it: Telegram → Settings → Devices → **Ampi reader (read-only)** → Terminate session, and `systemctl disable --now tg-reader`.

## 8. Todoist and Gmail (optional)

`ampi-tools` lets the assistant read and organize your Todoist and Gmail. The tokens live with a separate `ampitools` user that the assistant can't see. The assistant gets `todoist` and `gmail` commands (and skills explaining them).

**Approvals.** Reading, adding tasks, labelling, archiving and drafting happen directly. Critical actions are only queued: sending email, trashing, deleting, and changing, moving or completing existing Todoist tasks. You get a Telegram message with **✅ Approve / ❌ Reject** buttons, and the result appears under it. Only the bridge process can answer those requests, so the assistant can't approve its own. Change the policy in `/home/ampitools/config.toml` (root-owned): for example, let it move Todoist tasks without asking. Sending, trashing and deleting always ask.

1. `sudo bash deploy/install-tools.sh`
2. **Todoist:** Todoist → Settings → Integrations → Developer → copy the API token into `TODOIST_TOKEN=` in `/home/ampitools/env`.
3. **Gmail** (once, about 5 minutes):
   1. At <https://console.cloud.google.com/>, create a project (any name). Under **APIs & Services → Library**, enable the **Gmail API**.
   2. **OAuth consent screen** (Google Auth Platform): user type **External**, app name e.g. "Ampi", your email as the support and developer contact. Under **Audience**, add your Gmail address as a test user, then click **Publish app**. In "Testing" mode, Google expires the login after 7 days. Publishing doesn't need verification for your own use: you just click through an "unverified app" warning once.
   3. **Clients → Create client → Desktop app**. Copy the client ID and secret into `GMAIL_CLIENT_ID=` and `GMAIL_CLIENT_SECRET=` in `/home/ampitools/env`.
   4. Connect your account. This prints a link: open it, allow access, and paste back the `http://localhost:8765/?...` address that the browser ends up on (the page itself won't load):
      ```bash
      sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools gmail-login'
      ```
   The scope is `gmail.modify`: read, label, draft, send and trash. Permanent deletion isn't possible with it. To revoke: <https://myaccount.google.com/permissions>.
4. `systemctl restart ampi-tools`, then check:
   `sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools check'`
5. In `config.toml`, add `"Bash(todoist:*)"` and `"Bash(gmail:*)"` to `allowed_tools`, add a `[tools]` section with `approvals = true` (see `config.example.toml`), and `systemctl restart claude-assistant`.

## 9. Watchers and briefings (optional)

Watchers let the assistant act without being asked. They check Gmail, Todoist, a Telegram chat, or the clock, and start Claude with instructions from `<workspace>/watchers/<name>.md`. Starter instructions are installed by `install-tools.sh` (from `templates/watchers/`). Ask the assistant to change them, or edit them yourself.

```toml
[[profiles]]
timezone = "Europe/Athens"

[[profiles.watchers]]
name = "gmail-inbox"        # instructions: watchers/gmail-inbox.md
kind = "gmail"              # gmail | todoist | telegram | schedule
query = "in:inbox"          # Gmail search / Todoist filter / Telegram chat name, @username or id
every_minutes = 15

[[profiles.watchers]]
name = "morning-briefing"
kind = "schedule"
at = ["08:00"]              # local times
days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
```

- The first check only records what already exists. Only items that arrive later are handled, so you won't get your whole backlog at once.
- Each run is a fresh Claude session. The report goes to your private chat, starting with 🔔 and the watcher's name, and only if there's something worth saying. Runs are logged in `chats/watch-<name>/`, and reports also go to the main chat's log, so you can reply to them normally.
- If a watcher keeps failing (e.g. Gmail isn't connected yet), you get one ⚠️ message, then nothing more until it works again.
- `telegram` watchers need the reader from section 7 (e.g. a channel you repost things to).

## Isolation and limits

- The service runs as `assistant`, sees no other home directories, and gets a read-only view of the rest of the filesystem.
- If your other projects live outside `/home` (e.g. `/srv`, `/opt`), add them to `InaccessiblePaths=` (`/opt`, `/srv` and `/var/www` are already hidden) in `/etc/systemd/system/claude-assistant.service`, then `systemctl daemon-reload && systemctl restart claude-assistant`.
- Memory is capped at 2 GB for the bridge plus all Claude runs; `max_concurrent_runs` in the config (default 2) limits parallel runs. Watcher runs take turns (one at a time), so they never use up every slot.
- Other users' processes are hidden from it, and it can't connect to services on localhost (other projects' databases or APIs).
- Claude may only edit files inside the profile workspace; a bare `Edit`/`Write` in `allowed_tools` is narrowed to `Edit(./**)`. Reading the bridge's secrets (`/proc`, `~/.config`, `~/.claude`, `config.toml`, `data/`) is always denied, and project MCP servers are ignored.
- The bridge process is marked non-dumpable, so the `claude` runs it starts can't read its environment or memory.
- The workspace's `.claude/` (skills, settings) and `CLAUDE.md` are root-owned, so Claude can't give itself new permissions or rewrite its persona.

## Updating

```bash
cd ~/claude-assistant && git pull && sudo bash deploy/install.sh
```
