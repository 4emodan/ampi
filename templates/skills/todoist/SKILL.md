---
name: todoist
description: Read and manage the owner's Todoist (tasks, projects, sections, labels, comments, completed tasks) with the todoist command. Use for anything about their to-dos, plans, deadlines, daily/weekly briefings, inbox triage or productivity reviews.
---

# The owner's Todoist

Run `todoist` with the Bash tool: one command per call, no pipes or `&&`.

## Reading (always allowed)

```
todoist tasks [-f FILTER] [-p PROJECT] [-n 200]   active tasks: [id] content · project / section · due · p1 · @labels
todoist task ID                                   one task with its comments
todoist projects                                  projects and their sections, with ids
todoist labels
todoist completed [--since 2026-11-13T00:00:00Z] [--until ...]   (default: last 7 days)
```

Filters use Todoist syntax: `today | overdue`, `7 days`, `#Inbox`, `##Work` (with sub-projects),
`@waiting`, `p1`, `no date`, `!assigned`, `created after: -3 days`, combined with `&`, `|`, `!`.

## Adding (allowed)

```
todoist add "Call the bank" [-p Project] [-s Section] [--due "fri 10am"] [--deadline 2026-12-01]
            [--priority p1..p4] [-l label -l label2] [-d "description"] [--parent TASK_ID]
todoist new-project NAME [--parent PROJECT]     todoist new-section PROJECT NAME     todoist new-label NAME
```

## Changing existing tasks (the owner approves each request)

```
todoist update ID [--content ..] [--due ..|none] [--deadline ..] [--priority ..] [-l ..|--no-labels] [-d ..]
todoist move ID [ID ...] -p PROJECT [-s SECTION]
todoist complete ID [ID ...]      todoist reopen ID      todoist delete ID [ID ...]
todoist comment TASK_ID "text"   (collaborators in shared projects may be notified)
todoist apply plan.json           many changes as ONE approval request
todoist approvals                 recent requests and what happened
```

These don't run right away. The owner gets Approve/Reject buttons in Telegram and sees the result
there. One request must fit on one screen (split big plans), and at most 10 can wait at once. Add `-m "why"` before the subcommand (`todoist -m "inbox triage" move ...`) so the owner knows
why. Don't repeat a request or look for another way to do it.

**Batch your changes.** When organizing, don't send twenty separate requests. Write a plan file in
the workspace (for example `todoist/plan.json`) and send it once with `todoist -m "Inbox triage" apply todoist/plan.json`:

```json
[
  {"op": "move", "id": "123", "project": "Work", "section": "Admin"},
  {"op": "update", "id": "124", "due": "tomorrow", "priority": "p2", "labels": ["errand"]},
  {"op": "complete", "id": "125"},
  {"op": "add", "content": "Book dentist", "project": "Personal", "due": "next week"}
]
```

## Working well

- Before reorganizing, look at `todoist projects` and `todoist labels`, and fit what exists.
  Create new projects or labels only when they're clearly needed.
- Keep your notes about the owner's system (projects, what labels mean, routines) in `notes/todoist.md`
  and read it first.
- Briefings: overdue, today, the next 3 days, p1/p2 items without dates, and what was completed
  yesterday. Keep them short, group them by project, and list the top 3 priorities first.
