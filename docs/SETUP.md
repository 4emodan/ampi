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

- `/home/assistant/.config/claude-assistant/env`: one line per bot, e.g. `TG_TOKEN_PERSONAL=123:ABC…`
- `/home/assistant/config.toml`: set `owner_ids` to your Telegram user ID; add a `[[profiles]]` block per extra bot.
- `/home/assistant/profiles/<name>/CLAUDE.md`: the profile's persona and rules (a template is created for `personal`).

Check it:

```bash
sudo -iu assistant bash -c 'set -a; . ~/.config/claude-assistant/env; ~/app/.venv/bin/claude-assistant --config ~/config.toml --check'
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

## Isolation and limits

- The service runs as `assistant`, sees no other home directories, and gets a read-only view of the rest of the filesystem.
- If your other projects live outside `/home` (e.g. `/srv`, `/opt`), add them to `InaccessiblePaths=` (`/opt`, `/srv` and `/var/www` are already hidden) in `/etc/systemd/system/claude-assistant.service`, then `systemctl daemon-reload && systemctl restart claude-assistant`.
- Memory is capped at 2 GB for the bridge plus all Claude runs; `max_concurrent_runs` in the config (default 2) limits parallel runs.

## Updating

```bash
cd ~/claude-assistant && git pull && sudo bash deploy/install.sh
```
