# Gmail inbox: keep it organized

New emails in the inbox are listed above. Use the gmail skill. First read `notes/gmail.md`, the
owner's rules; create it from the defaults below if it's missing.

For each email (open it with `gmail read` when the summary isn't enough):

1. **Needs the owner** (a person writing to them, a question, a deadline, bills, security alerts,
   travel changes): leave it in the inbox, star it, and label it `Action`. If it has a date or task,
   add it to Todoist (`todoist add ... -p Inbox`) with a link-free one-line description of the email.
2. **Useful later** (receipts, orders, bookings, statements): label it `Receipts`, `Travel` or
   `Finance`, mark it read, and archive it.
3. **Bulk** (newsletters, promotions, notifications, social): label it `Newsletters` or
   `Notifications` and archive it. Don't trash anything from a real person or anything that looks important.
4. **Obvious junk or phishing**: collect it and put it in ONE trash request (`gmail apply` with a plan
   file) at the end. The owner approves it.

Never send replies on your own. If a reply is clearly needed, save a draft (`gmail draft --reply-to ID`)
and mention it.

Reply to the owner only if something needs them: list the `Action` items (sender, subject, what's
needed, by when) and say in one line what you filed. If everything was routine, reply `NO_REPLY`.
When the owner corrects you, update `notes/gmail.md`.
