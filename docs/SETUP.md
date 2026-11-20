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
(Telegram keeps showing that device name from the original login, even now that it can also write; see 7b.)

### 7b. Writing in Telegram as you: send, edit, delete (optional)

With the `telegram` command Ampi can send messages, edit them and delete them **from your own account**.
The reader's login does the writing, but the assistant never reaches it: requests go through ampi-tools,
which alone can use the reader's second socket (`/run/tg-writer`, group `tgwrite`, and the reader also
checks the caller is the `ampitools` user).

- **By default every write is a one-time request**: you get the exact message, the chat (with its id, size and
  whether it's public) and Approve / Reject buttons. For a single message to a person or a small group there is
  also **✅ + always here**: approve it and let Ampi send there without asking for 30 days.
- **Per-chat permissions** in `/home/ampitools/config.toml` (root-owned; Ampi can't change it):
  ```toml
  [telegram]
  default = "ask"                  # or "never": Ampi can't even ask, except where allowed below
  [[telegram.chats]]
  chat = 123456789                 # the id from `tg-read chats` (ask Ampi); "me" = your Saved Messages
  name = "Anna"                    # just a label for you
  allow = ["send", "edit"]         # without asking (edit: only Ampi's own message within 10 minutes)
  [[telegram.chats]]
  chat = -1001234567890
  never = true                     # never write here, not even asking
  ```
  then `systemctl restart ampi-tools`. Ask Ampi "what are your Telegram permissions?" or "stop sending to Anna
  without asking" (it can remove "always" permissions, never add them).
- **Even where allowed, Ampi asks first** for: anything a watcher, group run, run with a forwarded message, or
  unknown process wants; links, @mentions, emails, phone numbers or numbers of 4+ digits; text starting with "/";
  people who aren't in your contacts, channels, groups over 50 members (or of unknown size); deleting; and editing
  your own (not Ampi's) messages. One write per request, at most 10 automatic messages per chat and 20 in total a
  day; **every automatic message is reported to you** in your chat with Ampi.
- What is left: inside your private chat, Ampi can still be misled by text it reads there (a pasted email, a
  web page) into sending a short plain message to a chat you allowed; you see it in the report right away.
  "Your private chat" means any `owner_ids` account. A command you approved that plants files Claude later
  loads (e.g. `~/.claude` settings) would also run as your private chat, so be careful approving commands
  that write outside the workspace.
- **Never possible**: writing to bots, to chats outside your chat list, to Telegram's login-code chat, or to
  the chats Ampi reads itself (its groups and watched chats: a message from you there would count as your
  command); formatting, files, forwarding, reactions, moderating other people's messages.
- **Account safety**: at most 10 writes a minute, 150 a day and 10 different chats a day. If Telegram signals
  spam or flood limits, writing pauses (24 hours after a spam signal) and you're told.
- Ampi's messages show as "Me (sent by Ampi)" when it reads chats, so it never mistakes them for yours.

Setup (after sections 7 and 8, with `[tools] approvals = true`): re-run `deploy/install.sh`,
`deploy/install-tg-reader.sh` and `deploy/install-tools.sh`, then add `"Bash(telegram:*)"` to `allowed_tools` in
`config.toml` and `systemctl restart claude-assistant`.

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

## 8b. YouTube (optional)

The `youtube` command works right away for public channels: their newest uploads (RSS), and title and channel of
any video. YouTube blocks video pages and transcripts from servers, so **transcripts aren't available**; the
assistant summarizes from the description, chapters and comments. For details, search, comments and your own
account, connect the official YouTube Data API (same Google Cloud project as Gmail):

1. In the Cloud project: **APIs & Services → Library → YouTube Data API v3 → Enable**.
2. **Public data (recommended first, 1 minute):** **APIs & Services → Credentials → Create credentials → API key**.
   Edit the key: under **API restrictions**, choose **Restrict key → YouTube Data API v3**. Put it in
   `YOUTUBE_API_KEY=` in `/home/ampitools/env`. This gives video details, search, comments and channel info.
3. **Your account (optional):** playlists, subscriptions and liked videos. Under **Data access** of the consent
   screen (section 8), add the scope `.../auth/youtube.readonly` (and `.../auth/youtube` if you want changes). Then:
   ```bash
   sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools youtube-login'
   ```
   It works like `gmail-login`; choose the channel if you have several. This is **read-only**. To let the assistant
   create playlists, add to them and (with your approval) remove videos or change subscriptions, log in again with
   `youtube-login --write`. That permission also covers things the assistant is never given (uploading, deleting
   your videos), so use it only if you want those features. If the consent screen is still in "Testing", the login
   expires after 7 days.
4. `systemctl restart ampi-tools`, check with `ampi-tools check` (as in section 8), add `"Bash(youtube:*)"` to
   `allowed_tools` in `config.toml`, and `systemctl restart claude-assistant`.

**Approvals.** Reading, creating a (always private) playlist and adding to a private playlist happen directly.
Adding to a public or unlisted playlist, removing from playlists, subscribing and unsubscribing always ask.
"Private" playlists are still visible to collaborators you invited.

**Limits.** The API allows 10,000 units a day. Ampi keeps separate daily budgets: about 30 searches, 40 changes,
3,000 lookups, and 1,000 for watchers (`/home/ampitools/youtube-quota.json`). Requests to youtube.com itself
(feeds, oEmbed) are rate-limited and pause for 30 minutes if YouTube starts refusing them.

## 8c. Threads and Instagram (optional)

Meta's official APIs only give an app **your own posts and the replies/comments under them** (plus stats).
No feed, no other people's posts, no search, no DMs, and Instagram only for **professional** (Creator or
Business) accounts: personal Instagram accounts have no API. Posting, replying, deleting and hiding always
come to you as Approve/Reject buttons first.

1. At <https://developers.facebook.com/apps> create an app ("Other" → **Business** type is fine for both).
   - **Threads:** add the use case "Access the Threads API". Under its settings add the permissions
     `threads_basic, threads_read_replies, threads_manage_insights` (and `threads_content_publish,
     threads_manage_replies, threads_delete` if you want posting), put `https://localhost:8765/` in
     **Redirect callback URLs**, then **App roles → Roles → Add people → Threads Tester** with your Threads
     username, and accept it in Threads (usually Settings → Account → **Website permissions**). Copy the *Threads app
     ID and secret* into `THREADS_APP_ID=` / `THREADS_APP_SECRET=` in `/home/ampitools/env`.
   - **Instagram:** add the product "Instagram" → **API setup with Instagram login**. Add your Instagram account
     as an **Instagram tester** (App roles) and accept it in Instagram (usually Settings → **Apps and websites** →
     Tester invites). Set the redirect URL `https://localhost:8765/` under **Business login settings**. Copy the
     *Instagram app ID and secret* into `INSTAGRAM_APP_ID=` / `INSTAGRAM_APP_SECRET=`.
   - If Meta refuses a localhost redirect, use an https address on a domain you own instead (the page doesn't
     have to exist) and set it as `META_REDIRECT_URI=` in the env file too.
2. Log in (prints a link; after allowing access, paste back the address the browser ends up on):
   ```bash
   sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools threads-login'      # add --write to allow posting
   sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools instagram-login'
   ```
3. `systemctl restart ampi-tools`, `ampi-tools check` (as in section 8), add `"Bash(threads:*)"` and/or
   `"Bash(instagram:*)"` to `allowed_tools` in `config.toml`, and `systemctl restart claude-assistant`.

Logins last 60 days and are renewed automatically while the server runs. If your Threads profile is
**private**, Meta ends the permissions 90 days after login anyway: the assistant tells you a week before.
Optional watchers (section 9): `kind = "threads"` (`query = "replies"`) and `kind = "instagram"`
(`query = "comments"`), with `allowed_tools = ["Read", "Bash(threads:*)"]` / `["Read", "Bash(instagram:*)"]`;
instructions in `watchers/social-replies.md`. More than 30 new items at once, or more than 24 runs a day,
become a one-line notice instead of a Claude run.

## 8d. Web browser (optional)

`browse` opens public web pages in a headless browser that runs JavaScript ([Obscura](https://github.com/h4ckf0r0day/obscura),
a small Rust engine), for sites WebFetch can't read and for exact full text or specific items on a page.
Read-only: no logins, cookies, forms or clicks. No stealth: sites that show a bot check (Cloudflare, Reddit,
X, Amazon, Google search…) are reported as blocked, and YouTube always goes through the `youtube` command.

```bash
sudo bash deploy/install-browser.sh
# then add "Bash(browse:*)" to allowed_tools in config.toml, and
systemctl restart claude-assistant
```

How it is contained (pages are hostile input, and the engine is a young third-party project whose
prebuilt release is pinned by sha256: that proves it's the published file, not who built it):

- Every page runs in a fresh `ampi-browser@` instance as the `ampibrowser` user, and everything it started
  is killed when the request ends. One page at a time, 600 MB memory cap, 90 s limit.
- The instance sees almost no files: no home directories, no `/etc` (apart from CA certificates and
  generated minimal files), `/var`, `/srv`, `/opt` or `/run`, and it can't open Unix sockets.
- An nft table (`inet ampi_browser`, loaded by `ampi-browser-net`) lets that user send only DNS to
  Hetzner's public resolvers (`/etc/ampi-browser/resolvers`) and TCP 80/443 to public addresses that
  aren't this server's own. Private/local addresses, other ports and other projects are unreachable.
  Every instance first checks the table is loaded unchanged and refuses to start otherwise (e.g. after
  `nft flush ruleset`: `systemctl restart ampi-browser-net` fixes it).
- **Watchers and groups never get `browse`**: their tool rules are stripped of anything that would allow
  it, because injected text could put private data into a URL. That includes a bare `Bash` rule and rules
  for commands that run other commands (`env`, `sh`, `python3`, `uv`, `xargs`, `timeout`…), which those
  runs lose entirely. The service also refuses requests marked as coming from those runs.
- Long pages are saved under `web/` in the workspace (deleted after a day), so Claude can Grep them.

Logs: `journalctl -u 'ampi-browser@*'` (site name, outcome and time only). Check: `browse status` as the
assistant (it runs the engine with JavaScript on a local test page). To update the engine, change `VERSION`/`SHA256` in `install-browser.sh` and run it again; the
previous version stays in `/usr/local/lib/obscura/` and is switched back automatically if the new one
doesn't start.

## 9. Watchers and briefings (optional)

Watchers let the assistant act without being asked. They check Gmail, Todoist, a Telegram chat, YouTube channels, or the clock, and start Claude with instructions from `<workspace>/watchers/<name>.md`. Starter instructions are installed by `install-tools.sh` (from `templates/watchers/`). Ask the assistant to change them, or edit them yourself.

```toml
[[profiles]]
timezone = "Europe/Athens"

[[profiles.watchers]]
name = "gmail-inbox"        # instructions: watchers/gmail-inbox.md
kind = "gmail"              # gmail | todoist | telegram | youtube | threads | instagram | schedule
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
- `youtube` watchers report new uploads (no Shorts) from channels. Channels added later are taken in quietly,
  without their backlog. `subscriptions` needs `youtube-login`; a list of channels works without any setup:
  ```toml
  [[profiles.watchers]]
  name = "youtube-uploads"    # instructions: watchers/youtube-uploads.md
  kind = "youtube"
  query = "@veritasium, @3blue1brown"   # or "subscriptions" (at most 200 channels are checked)
  every_minutes = 60
  allowed_tools = ["Read", "Bash(youtube:*)"]  # titles are written by strangers: no writing files, no web
  ```

## 10. Running scripts and installing things, with your approval (optional)

With `ask_permissions = true` in a profile, anything Claude wants to do that `allowed_tools` doesn't allow
(running a script or program, `uv pip install`, fetching a page when the web isn't allowed...) reaches you
as a Telegram message with **✅ Approve / ❌ Reject**. It shows the exact command or request, and the run
waits for your tap, up to 10 minutes. No answer means no.

- Approved commands run as the `assistant` user inside the same sandbox, so they can only write to
  `/home/assistant`. Python packages go into a virtualenv in the workspace.
- The deny rules still win: the secrets listed under "Isolation and limits" are never even asked about.
- Requests from a group run are marked, because that run handles messages written by others.
- Watchers never ask: whatever their rules don't allow is simply refused.
- A request can't be approved by anyone but you: buttons carry a one-time secret and only count on the
  message the bot sent. `/cancel` withdraws open requests.

**Ubuntu packages (apt).** Claude can also ask for system packages (e.g. `librsvg2-bin`). The request
shows the packages, anything apt would add along with them, and the reason. Approved installs are done
by a small root helper that only takes requests from the bridge process, only installs (never removes),
and logs everything (`journalctl -u ampi-pkg`):

```bash
sudo bash deploy/install-pkg.sh
# then in config.toml, in the profile: ask_permissions = true
systemctl restart claude-assistant
```

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

If you use them, also re-run `deploy/install-tools.sh` and `deploy/install-browser.sh`.
