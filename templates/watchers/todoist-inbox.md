# Todoist inbox: triage new tasks

New tasks in the Todoist Inbox are listed above. Use the todoist skill. First read `notes/todoist.md`
(the owner's projects, labels and habits); if it's missing, look at `todoist projects` and
`todoist labels` and write it.

For each new task, work out where it belongs and what it needs:
- the project and section (only ones that exist, unless it clearly needs a new one)
- a clearer title if it's vague ("dentist" → "Book dentist appointment")
- a due date if the owner wrote one in the text, a priority if it's obviously urgent, useful labels
- for big tasks, 2–4 subtasks you could add (`todoist add ... --parent ID`), which is safe

Moving or changing tasks needs the owner's approval, so put all the changes into ONE plan file and send
it with `todoist -m "Inbox triage" apply todoist/plan.json`. Then reply with one short line per task
("Book dentist → Health, due Fri"), so the owner knows what they're approving.

If a task is already well placed (has a project and date, or the owner is clearly using the Inbox on
purpose), leave it alone. If nothing needs changing, reply `NO_REPLY`.
