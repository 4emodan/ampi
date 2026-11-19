# Clipboard channel: organize what the owner saves

New posts in the owner's Telegram channel, where they repost anything interesting, are listed above.
Handle them like the Clipboard group (see `groups/clipboard.md` if it exists). For each item:

1. Work out what it is (article, video, tool, recipe, idea, event, place, product, quote…).
2. If it has a link, read it (WebFetch) so you can summarize it.
3. Append it to `notes/clipboard/YYYY-MM.md`: date, type, title, link, a 2–3 sentence summary, and tags.
   Check for duplicates first and note them.
4. If it implies a task or a date (an event, a deadline, "try this"), add it to Todoist with
   `todoist add ... -p Inbox` and the link in the description.

Reply with one line per item ("📌 Article: … #tag") only if something deserves attention (a dated
event, a task you added, a duplicate). Otherwise reply `NO_REPLY`.
