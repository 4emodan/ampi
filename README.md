# Claude Assistant

A personal AI assistant you talk to in Telegram. It runs on your own server and uses the official Claude Code CLI signed in with your Claude subscription, so there's no API bill.

- One Telegram bot per **profile** (e.g. personal, work), each with its own workspace, persona, memory and files.
- Each **topic** in a bot chat is a separate conversation with its own Claude session.
- Photos and files you send are saved to the workspace for Claude to read; Claude can send files back.
- Long tasks (research) run in the background with a typing indicator; `/cancel` stops them.

Setup: [docs/SETUP.md](docs/SETUP.md). Requirements: [REQUIREMENTS.md](REQUIREMENTS.md). Constraints: [CLAUDE.md](CLAUDE.md).

## Status: MVP

Done: Telegram chat bridge, profiles, topics as conversations, groups (acts on everything you post or forward, e.g. a Clipboard group), attachments in and out, conversation logs shared across sessions (ask in one chat about another), per-conversation model, run log, systemd deployment with isolation.

Not yet: scheduled tasks, email monitoring, Gmail/Calendar/Todoist/YouTube integrations, approval buttons for sending/deleting, voice transcription, streaming replies.

## Development

```bash
uv sync
uv run pytest
```

Tests use a fake `claude` CLI (`tests/fake_claude.py`) and a fake Telegram API, so they need no network or Claude login.
