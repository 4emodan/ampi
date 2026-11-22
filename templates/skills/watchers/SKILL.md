---
name: watchers
description: Create, change, inspect and manage watchers (automations that run on their own when something happens or at set times). Use when the owner asks to watch, monitor, track, notify on, check regularly, summarize automatically, or "every time X happens do Y".
---

# Watchers

A watcher wakes on triggers (a time, a new email, a new Telegram message, a new Todoist task, a YouTube upload, a
web page changing) and runs a pipeline of steps: scripts, AI steps, actions (Todoist, Gmail, Telegram…) and
messages to the owner. Approved watchers are kept by ampi-tools; you can't change what an approved watcher does.

## Creating or changing one

Watchers are built by a separate builder that always uses the strongest model. Don't write watcher files yourself.

```bash
watchers build NAME "what the owner wants, with every detail they gave"
watchers build NAME --change "what to change"
```
- NAME: short, lowercase, e.g. `pascal-school-mail`.
- Only from the owner's own private chat (it records their message as the original request).
- The builder reports back in this chat and the owner gets the approval request with buttons. Tell the owner
  that's coming; don't build twice.

## Seeing what exists

```bash
watchers list            # all watchers: status, risk 🟢🟡🔴, today's runs
watchers show NAME       # what it does, permissions, limits
watchers runs NAME       # recent runs
watchers logs RUN_ID     # every step of a run
watchers space NAME      # its saved data
watchers audit NAME      # actions it took
```
`watchers/INDEX.md` and `watchers/<name>/STATUS.md` are summaries you can just read.

## Managing

```bash
watchers pause NAME      # stop it (the owner resumes with /watcher NAME)
watchers run NAME        # run it now
watchers retry RUN_ID    # retry a failed run
```
Resuming, deleting and approving are the owner's buttons (/watchers, /watcher NAME).
