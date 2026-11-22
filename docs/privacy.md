---
title: Ampi privacy policy
---

# Ampi privacy policy

*Last updated: 22 November 2026*

Ampi is a self-hosted personal assistant. Each installation is run by one person (the "owner") for their own use,
on a server they control. The only user whose data Ampi accesses is that owner. There is no shared service, no
company behind it, and no other users.

## Data Ampi accesses

Only what the owner connects, with the owner's explicit consent through each provider's sign-in screen:

- **Google user data (Gmail)**: email messages, labels and drafts of the owner's own Gmail account, through the Gmail
  API with the `gmail.modify` scope. Optionally YouTube data (playlists, subscriptions) through the YouTube Data API.
- Messages the owner sends to their own Telegram bot, and other services the owner chooses to connect (Todoist,
  Telegram, Google Maps, Wolt, Threads, Instagram).

## How it is used

Google user data is used only to provide features the owner asks for, to the owner:

- reading, searching and summarizing the owner's email for them;
- labelling, archiving and drafting email on the owner's instruction;
- sending or trashing email only after the owner approves each such action in Telegram;
- automations ("watchers") the owner has set up and approved, such as notifying the owner about new email.

## Where it goes

- **Stored**: on the owner's own server. OAuth tokens are kept by a separate system user that the assistant itself
  cannot read. Nothing is sent to the authors of this software.
- **Processed by an AI model**: to answer the owner, relevant content (for example an email the owner asked about)
  is sent to Anthropic's Claude through the owner's own Claude account, under Anthropic's terms for that account.
- **Not shared** with anyone else, **not sold**, **not used for advertising**, and **not used to train AI models**
  by this software.
- **Not read by humans** other than the owner.

Ampi's use and transfer of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Keeping and deleting data

The owner decides what is kept. Revoking access at any time: Google account → Security → Third-party apps
([myaccount.google.com/permissions](https://myaccount.google.com/permissions)). Deleting the tokens and logs on
the server removes Ampi's copy.

## Contact

Questions about this policy: open an issue in the [source repository](https://github.com/4emodan/ampi/issues).
