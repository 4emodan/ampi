# Topics, reactions

**Topics.** Every conversation (the private chat, its topics, groups and theirs) can have a role and limits of its own. `/topic`
there shows them: `/topic role TEXT`, `/topic changes ask|off|on`, `/topic off|ask|on ADAPTER`, `/topic watcher NAME`, `/topic reset`.
A topic can only be stricter than everywhere else. Approval requests of a topic of the private chat are shown in it. Kept by
ampi-tools (`permissions.py`), so the dashboard (Topics, Abilities) and `/topic` change the same thing. In a limited topic the
`watchers` command only lists and pauses. Not separated: notes, logs of other topics, workspace files.

**Reactions.** 👀 while Ampi works, then its own choice, 👌 or 👎 (private chats). Ampi may react to your messages of the same
conversation (`REACT: 👍` at the end of its reply, `reactions.py`). `/react off` per topic.
