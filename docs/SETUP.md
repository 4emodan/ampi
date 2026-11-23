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

Send your bot a message. Commands: `/new [title]`, `/reset`, `/cancel`, `/model [name|default]`, `/persona`, `/status`, `/help`.

`/persona butler` (or your own words, e.g. `/persona dry humour, in Russian, at most five sentences`) sets the tone and character of the replies; `/persona` shows what is set and the ready-made ones, `/persona off` goes back. Only your own command in the private chat changes it, and it changes only how Ampi talks.

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

`/protect` in a chat or topic (private or group): Ampi answers there only when you tag it (`@your_bot`) or reply to one of its messages, and neither reads nor keeps anything else. `/protect chat` covers a chat with all its topics; `/protect off` / `/protect chat off` end it.

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
  web page, or a forwarded message from an earlier turn of the same conversation, which only the turn that
  contains it treats as outside content) into sending a short plain message to a chat you allowed; you see it
  in the report right away. `/new` starts a fresh conversation.
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

## 8b-2. Google Maps (optional)

The `maps` command uses Google's API for AI assistants ("Maps Experience agent tools"): place search with a
summary (ratings, address, open now), travel times (drive, walk, bike, public transport), weather, and explaining
Google Maps links you send. Without a key, the assistant can still give you Google Maps search and directions links.

1. In a Google Cloud project (the Gmail one is fine): **Billing → link a billing account**. Google requires it,
   but its free monthly allowance (10,000 basic and 5,000 detailed requests) covers personal use.
2. **APIs & Services → Library → "Maps Experience agent tools" → Enable.**
3. **Set hard caps so it can never cost money:** **APIs & Services → Maps Experience agent tools → Quotas &
   System Limits**, and lower the per-day limits to about **300** requests. Budgets alone only send e-mails;
   quotas stop requests.
4. **Credentials → Create credentials → API key.** Edit it: **API restrictions → Restrict key → Maps Experience
   agent tools**, and **Application restrictions → IP addresses →** the server's IPv4 and IPv6.
5. In `/home/ampitools/env`:
   ```
   MAPS_API_KEY=AIza...
   MAPS_LANGUAGE=en          # language of the answers
   MAPS_REGION=FI            # your country (two letters), for ambiguous names
   MAPS_NEAR=60.1699,24.9384 # optional fallback; better: your home in personal info (`vault-set home`, section 8e)
   MAPS_TIMEZONE=Europe/Helsinki  # fallback; personal info's timezone wins
   ```
6. `systemctl restart ampi-tools`, `ampi-tools check`, add `"Bash(maps:*)"` to `allowed_tools`, restart
   `claude-assistant`.

Ampi keeps its own budget below Google's free allowance (`/home/ampitools/maps-budget.json`, see `maps status`):
about 250 basic and 40 detailed requests a day. Answers always end with Google Maps source links, as Google's terms
require. Google Maps and Wolt pages are not opened in the web browser; they have their own commands.

## 8b-3. Wolt (optional)

Wolt has no public API for customers, so the `wolt` command uses the same interface wolt.com's website uses.
That is **unofficial and against Wolt's terms for automated use**: Wolt may change it at any time or block the
server. Ampi keeps it gentle (about one request a second, a few hundred an hour, an honest user agent) and stops
for an hour at the first refusal.

Without logging in, the assistant can find venues and dishes near you and read menus, opening hours and delivery
estimates. Set your area in `/home/ampitools/env` (else `MAPS_NEAR` is used):
```
WOLT_NEAR=60.1699,24.9384
WOLT_LANGUAGE=en
```

**Your account (optional):** your orders, order status, and filling your basket. **Ampi never places or pays for an
order** (Wolt requires a device fingerprint for that, which we don't fake): it fills the basket, and you check out
in the Wolt app. Replacing a basket you already have at that venue, or a basket requested from a group or watcher
run, asks you first. To connect:
```bash
sudo -u ampitools -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools wolt-login'
```
It asks for the `__wrtoken` cookie from a private browser window where you logged in to wolt.com (instructions are
printed). Wolt changes this token on every use; the newest is kept in `/home/ampitools/wolt-token.json`. If it ever
stops working, run `wolt-login` again. Then add `"Bash(wolt:*)"` to `allowed_tools` and restart both services.

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

## 8c-2. Instagram and Threads through your own web session (optional, risky)

The official APIs above can't read your feed, other people's profiles or posts, stories or messages, and don't
work for a personal Instagram account at all. This section lets Ampi use **your own logged-in web session**
instead, the way instagram.com and threads.com use it in your browser.

**Risks you accept:** this is against Meta's terms. Instagram or Threads may ask you to confirm it's you
(a "checkpoint"), lock the account for a while, or ban it. That is more likely here because the requests come from
a Hetzner datacenter address far from where you usually log in. Ampi keeps it small and honest: one fixed user
agent that says what it is, about one request a second at most, at most 120 reads an hour and 600 a day, 20
changes an hour and 60 a day, 20 follows a day, and it never pretends to be a browser, never solves a challenge and
never touches account settings. Only the requests listed in `src/ampi_tools/meta_session.py` (`ENDPOINTS`,
`TH_DOCS`) are ever sent. **They have not been checked against the real sites yet**; see "First session" below.

### Connect (cookies, never your password)

1. In a desktop browser, log in at <https://www.instagram.com> yourself (for Threads: <https://www.threads.com>;
   it has its own session).
2. Open the developer tools (F12) → **Application** (Chrome/Edge) or **Storage** (Firefox) → **Cookies** →
   `https://www.instagram.com`. Keep the tab open: you'll copy three values, `sessionid`, `csrftoken` and
   `ds_user_id` (optionally also `mid`, `ig_did` and `datr`, your browser's own device cookies; never made up).
3. On the server:
   ```bash
   sudo -u ampitools -H bash -c '~/app/.venv/bin/ampi-tools instagram-session'   # or threads-session
   ```
   Paste each value when asked (nothing is shown as you paste). Ampi checks that the session is the account in
   `ds_user_id` (one request; never the endpoint that returns your email/phone), then saves it in
   `/home/ampitools/instagram-session.json` (mode 600). The running service picks it up without a restart.
4. **Don't log out** in that browser (it would end this session too); just close the tab. Add exactly
   `"Bash(instagram:*)"` / `"Bash(threads:*)"` to `allowed_tools` if they aren't there yet (other spellings don't
   switch social mode on for that command).

`ampi-tools check` shows each session (on / paused / cooling down). `ampi-tools instagram-session --forget`
deletes the saved session; logging out of all sessions in the Instagram app ends it on Meta's side too.

### When Instagram asks to confirm it's you

Any redirect (to a login or challenge page), a 401/403, `checkpoint_required` / `challenge_required` /
`login_required`, a removed session cookie or a refused user agent **pauses** that session: every request fails
with "Instagram wants you to confirm it's you…", the DM watcher stops quietly, and you get **one** Telegram notice.
Open the Instagram app (or website), do what it asks, then run
`sudo -u ampitools -H bash -c '~/app/.venv/bin/ampi-tools instagram-session --resume'` (or save fresh cookies with
`instagram-session`). A "please slow down" answer (429, `feedback_required`) pauses reads for an hour; on a change
(comment, like, follow) it pauses until you resume.

### Social mode: reading in chat

Feeds, profiles, bios, captions, comments, replies and stories are written by strangers, so Ampi only reads them in
a conversation that **can't use the web**: send `/social` in a topic of your private chat. From then on, until
`/new` (or `/reset`), that conversation has **only** the instagram and threads commands, as tools without a shell: no
web, no files (it can't open your attachments, notes or other chats), no other services, no plans or `--text-file`, no
permission prompts, and none of the profile's other rules or `extra_args`. Every action it asks for comes to you with a ⚠
("asked for by a social-mode conversation"), and comments, likes and follows can only be asked for from there.
In any other conversation Ampi answers
that it can't read Instagram there and asks you to send `/social`. Only your own `/social` switches it on. Tip:
keep one topic called "Social" for this.

In social mode: `instagram feed`, `instagram profile USER`, `instagram posts USER`, `instagram post LINK`
(with comments), `instagram stories`, `instagram story USER` (nothing is marked as seen, media isn't downloaded),
`threads feed`, `threads profile USER`, `threads posts USER`, `threads post LINK` (with replies).

Changes **always** come to you as Approve/Reject, showing exactly what will happen, and are checked again just
before they run (if the post's caption, the comment or the account changed, nothing is done): `instagram comment
LINK "TEXT" [--reply-to COMMENT_ID]`, `instagram like|unlike LINK [--comment ID]`, `instagram follow|unfollow
USER`, `instagram delete-comment ID --post LINK` (your own comments, or anyone's on your posts), `threads
like|unlike LINK`, `threads follow|unfollow USER`. A comment containing something stored in your personal info is
flagged with ⚠ in the request. Posting on Threads stays on the official API (section 8c). Not possible: sending
DMs, search, saved posts, follower lists, posting media.

### Direct messages: only a watcher, never a chat

Ampi never reads your DMs in a conversation. A watcher does: the daemon reads the inbox (no AI), a tool-less AI step
summarizes the new messages as untrusted data, and the summary comes to your chat as a watcher message (links, code,
addresses, @names and /commands in it are broken up). Message requests and group chats you never wrote in are left out,
nothing is marked as read, and it doesn't check at night (23:00-08:00 in your home timezone; change `quiet` in the file).
To set it up:
```bash
sudo -u assistant -H bash -c 'mkdir -p ~/profiles/personal/watchers && cp -r ~/app/templates/watchers-v2/instagram-dms ~/profiles/personal/watchers/'
```
then ask Ampi in your private chat: *"build the watcher instagram-dms from the files that are already in
watchers/instagram-dms, without changing them"*. You get the usual approval request (🟢 low risk: it only reads
and messages you); its test run uses made-up messages, not yours. It checks every 15 minutes (4 of the 30
watcher reads an hour the session allows); `instagram dms` in chat, or **Run** in `/watcher instagram-dms`, checks
now (Run in `/watcher` also during quiet hours; `instagram dms` from chat or social mode keeps quiet hours, and
from social mode at most once per 10 minutes); the summary only ever goes to you.
Messages count as reported only after the
message to you was sent: if a run fails they are reported again (you may see some twice), not lost. A big backlog comes
in several messages; messages the AI couldn't handle twice (or Telegram refused three times) are only counted ("see the app") so they don't
hold up newer ones, while outages and usage limits just wait; when Instagram didn't send everything, the message says so and you look in the app.

**What is kept.** Social conversations aren't written to `chats/`, long results are cut instead of saved, and their
approval requests and results are visible only to social conversations (and you in Telegram). Runs of the DM watcher keep
step names, times and statuses, no texts; its saved data is times and message ids; its AI call leaves no transcript. What
does exist: the messages in your Telegram, the approval requests in `/home/ampitools/approvals.sqlite3`, and Claude Code's
own session file of a social conversation under the assistant's `~/.claude` (needed to continue it, and not
deleted by `/new`; Ampi's file rules block it, but a command you approve with a tap could read it). Forwarding or quoting such a message in another conversation
gives Ampi a note instead of its text. What can't be detected: text you copy and paste, forward as a copy ("hide
sender's name") after changing it, or put in a screenshot; and messages your bot posts in groups (group runs never see
social text themselves).

**Reading your Telegram (section 7).** `tg-read` can't read your chat with Ampi's own bots: not in lists, history, search,
watchers or forwards. ampi-tools tells tg-reader which bots those are (from the bridge, with `[tools] approvals = true`),
and tg-reader remembers them. With ampi-tools installed, tg-reader reads nothing until it was told at least one bot (so `[tools] approvals` must be on), so update both
(`install-tools.sh`, `install-tg-reader.sh`) and restart claude-assistant; saving a session is refused until this works.
Files that earlier `tg-read` calls saved aren't touched: look through `~/profiles/*/telegram/` (and `chats/`,
`instagram/`, `threads/`) yourself and delete what you don't want later conversations to read.

A change (like, comment, follow) that got no clear answer is reported as "may have happened" and never repeated: look
before asking again.
To stop it: `/watcher instagram-dms` → Pause or Delete.

### First session: what to check

The endpoints and GraphQL ids come from public knowledge of the web apps. Try one read of each kind in a social
topic. If something fails with "changed its web app; the `X` query needs updating", or a request is refused,
open DevTools → **Network** on the same page in your browser, find the request the site itself makes, and correct
its path, `doc_id` or name in `ENDPOINTS` / `TH_DOCS` / `IG_DOCS` in `meta_session.py`. GraphQL entries marked
`"placeholder"` are never sent: until you fill in the real `doc_id` (and set the status to `"known"`), Ampi answers
"this … query isn't known yet: copy its doc_id from DevTools". That currently covers the Instagram home feed
(`PolarisFeedRootPaginationCachedQuery_subscribe`) and, on Threads, the feed, looking up a username (so profiles,
posts and follows), likes and follows. Entries marked `"stale"` are real but from 2023 and are sent; correct them if
they fail. Don't change the user agent: if Meta's web endpoints refuse non-browser clients ("useragent mismatch"),
the session stops by design. It also stops on a threads.com page without its tokens, whatever the reason.

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

## 8e. Personal info and payments (optional)

Ampi can fill your name, email, phone and address into web forms, and buy things with your card, in a separate
**checkout browser**. She never sees the card: the server types it into the payment provider's field (Stripe,
Adyen, Braintree, Shopify, Paytrail…) and presses Pay only after you approve that exact purchase in Telegram (site,
amount, what). Your details are filled the same way after you approve, or without asking on a site you allowed
with "✅ + always here" (30 days, exact site). Only your own chat can use any of this, never groups or watchers.

**Use a virtual card** with its own spending limit, ideally a per-purchase limit equal to `max_amount` below and
bank-app confirmation for each payment. The bank app (3-D Secure) shows the real amount and merchant: that's your
final check.

1. Install the checkout browser (its own users, `ampicheckout` for the engine and `ampicheckoutctl` for the
   controller, and its own firewall table):
   ```bash
   sudo bash deploy/install-checkout.sh
   ```
2. Enter your details on the server, as a **personal info directory** of named entries: `me` (person), places
   (`home`, `work`, …), cards (`card-main`, …) and notes. Secret card values aren't shown as you type.
   ```bash
   T='sudo -u ampitools -H bash -c'
   $T 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools info-set me'          # name, email, phone, birthday
   $T 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools info-set home'        # address, location, timezone
   $T 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools info-set work'        # company, address, location
   $T 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools info-set card-main'   # number, expiry, CVC, name, label
   $T 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools info-show'            # masked
   systemctl restart ampi-tools claude-assistant
   ```
   `info-set NAME FIELD` changes one field (e.g. `info-set work address.line2`), `info-delete NAME` removes an entry
   and its site permissions, `info-default-card NAME` picks the card used when Ampi doesn't name one,
   `info-search PLACE on|off` decides whether Maps/Wolt/routes may use a place (on by default for every place), `info-reveal NAME on|off`
   decides whether Ampi may ask to see an entry. Locations: latitude,longitude (right-click in Google Maps) or an
   address (looked up once with your Maps key). Stored in `/home/ampitools/vault.json` (only that user can read it);
   the old `vault-set profile|card|home` commands still work. `/info` in Telegram shows the entries (masked) and the
   site permissions, with buttons to remove them.

   Ampi can also **save places** you tell her about ("save this as my gym"): each save is an approval request showing
   the name and the full address (and whether it replaces an existing place); the location is looked up once on
   Google Maps. She can't create other kinds of entries, delete anything or change switches.

   How each use works:
   - **Ampi sees a value** (`info reveal`): asks you every time; cards, locations and timezones never.
   - **Filling a form**: asks; "✅ + always here" = exactly those fields on exactly that site for 30 days. Values stay
     masked in what Ampi reads from that site.
   - **Paying**: the card is used without a separate tap, but every purchase asks (shop, amount, what, which card),
     unless a watcher's approval pre-approved it (fixed shop, card, caps per purchase/week/month; reported afterwards).
   - **Searches near a place** (Maps, Wolt, `maps route work home`, `wolt venues --at work`): automatic for places with searches on, only in your
     private chat and for watchers that declare it; coordinates never appear in what Ampi sees.
   - **Timezone**: used by schedules and Maps. `MAPS_NEAR`/`WOLT_NEAR`/`MAPS_TIMEZONE` and the profile's `timezone`
     stay as fallbacks.
3. Limits in `/home/ampitools/config.toml`:
   ```toml
   [payment]
   max_amount = 100      # per purchase, refused above (in the card's currency)
   currency = "EUR"
   max_per_day = 3
   # psp_origins = ["https://pay.example-provider.com"]   # extra payment providers whose fields may get the card
   ```
4. `systemctl restart ampi-tools`, add `"Bash(info:*)", "Bash(checkout:*)", "Bash(payment:*)"` to `allowed_tools`,
   and `systemctl restart claude-assistant`.

What to expect: many shops block server browsers (bot checks, which Ampi never tries to get around) or need an
account. Ampi uses guest checkout only. After paying, the session is read-only; 3-D Secure is confirmed by you in
your bank app. `payment log` lists purchases (card shown masked). If a payment step times out, Ampi is told it MAY
have gone through and not to pay again before checking.

## 8f. The hub: settings pages in your browser

`ampi-hub` (installed and started by `install-tools.sh`) serves your settings pages; the first is the **address
book** (home, work and other places: address, company, location, timezone, the search and reveal switches). It has
no network port: it listens on `/run/ampi-hub/hub.sock`, which only root and `ampitools` can open (Ampi can't), and
you reach it through SSH from your computer:

```powershell
.\scripts\ampi-hub.ps1                  # or: -Server <ssh alias> -Port 8766
```

The script reads the hub's access token over SSH (new at every hub restart), forwards `127.0.0.1:8765` to the
socket, checks that the port is really the tunnel's, and opens the page; keep its window open while you use it.
It needs an SSH host alias (`pipa-home` by default) that logs in as root, ideally with a key in ssh-agent (otherwise
you type the passphrase twice). Changes apply at once (no restart). "Find from address" looks the location up on
Google Maps (needs `MAPS_API_KEY`; at most 30 lookups a day from the hub). The page refuses other hostnames and
cross-site requests, so a site open in your browser can't use the tunnel.

## 8g. Companion app: your Android phone (optional)

Notifications, alarms and timers on your phone, asked for in chat ("wake me at 7"). Android 16 or later. How it works and
what protects it: [COMPANION.md](COMPANION.md).

1. Server: `sudo bash deploy/install-phone.sh`. Nothing listens on the network until a phone is paired; pairing the first
   phone opens TCP port 28471 (in `ufw`, if you use it; a Hetzner Cloud firewall needs the same rule), and removing the
   last phone closes it.
2. Your PC (Android SDK and Android Studio installed): `.\companion\build.ps1 -Release` builds the app and, the first
   time, makes your signing key in `%USERPROFILE%\.ampi-companion`. Keep a copy of that folder offline.
3. Phone: Settings → About phone → tap Build number 7 times; Developer options → USB debugging. Connect the cable, then:

   ```powershell
   .\scripts\companion.ps1 -Install -Pair
   ```

   Confirm on the phone, compare the code on both screens, answer yes on the PC, then press "The codes match". Switch USB
   debugging off again. The app shows what it still needs (notifications, background, full-screen alarms).
4. Server: add `"Bash(phone:*)"` to `allowed_tools` in `/home/assistant/config.toml`, then
   `systemctl restart claude-assistant`.

`ampi-phone status` and `ampi-phone devices` (root) show the state; **lost phone:** `ampi-phone remove phone`. Updating the
app: build again and `-Install`; the pairing stays. `sudo bash deploy/install-phone.sh --uninstall` removes everything.

## 9. Watchers (automations)

A watcher wakes on **triggers** (a time, a new email, a new Telegram message, a new Todoist task, a YouTube
upload, a web page changing) and runs a **pipeline** of steps: scripts, AI steps, actions (Todoist, Gmail,
Telegram…) and messages to you. Ask Ampi in your private chat ("send me a summary of every email from Pascal
school"); a builder that always uses Opus writes it, tests it on recent real items, and you get one approval
request with what it does, its risk (🟢🟡🔴), what it may do, and the test result:

- **✅ Turn on and allow all**: it acts on its own from then on (until it changes or you delete it).
- **▶️ Turn on, ask each time**: every change it wants asks you (✅ Approve / ✅ Allow for this watcher / ❌ Reject).
- 🔴 high-risk watchers (agent steps that act, targets decided at run time, sending email, deleting, payments) need
  a second tap and report every action they take.

**Who holds what.** Approved watchers (the exact files, their hash, what they may do, their saved data and every
action they took) are kept by ampi-tools, which Ampi can't reach. The bridge runs them from that frozen copy;
editing `watchers/<name>/` in the workspace only prepares a new version for your approval. Scripts run in their
own sandbox (`ampi-script@`, user `ampiscript`: no network, no files). Shared helper scripts are in
`watchers/lib/` (root-owned).

**Seeing them.** `/watchers` and `/watcher NAME` (Pause, Resume, Run now, Delete) in Telegram;
`watchers/INDEX.md` and `watchers/<name>/STATUS.md`; Ampi's `watchers list|show|runs|logs|space|audit` commands.
Full run details are in `/home/assistant/data/watch-runs/` (60 days).

**Limits.** Each watcher has daily caps (runs, AI calls, actions; the builder sets them); reaching one pauses it
until the next day with one message. 5 failed runs in a row pause it as "failing". Watcher AI steps share one slot
for short structured calls and one for agent steps, so your chats keep theirs.

Setup (after section 8, with `[tools] approvals = true`):
```bash
sudo bash deploy/install-script-sandbox.sh
sudo bash deploy/install.sh && sudo bash deploy/install-tools.sh
# config.toml: add "Bash(watchers:*)" to allowed_tools; max_concurrent_runs = 3 is recommended
systemctl restart ampi-tools claude-assistant
```
The old `[[profiles.watchers]]` are converted once on the first start: each comes to you as an approval request
("ask each time" keeps today's behaviour). After approving them, delete the `[[profiles.watchers]]` blocks.

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
