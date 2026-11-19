"""Todoist and Gmail for the assistant, with owner approval for critical actions.

The tokens live with a separate `ampitools` Linux user that the assistant can't see. A daemon
(server.py) holds them and answers requests over a Unix socket. Every request is an *operation*
(ops.py). Safe operations (reading, adding, labelling) run at once. Critical ones (sending, deleting,
changing existing Todoist tasks) are only queued: the bridge shows them to the owner in Telegram
with Approve/Reject buttons, and only the bridge's own process may answer (checked by its PID).
"""
