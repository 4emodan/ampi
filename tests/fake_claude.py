"""Stand-in for the `claude` CLI used by the tests.

Behaviour is chosen with FAKE_CLAUDE_MODE:
  echo     reply with the prompt and the arguments (default)
  missing  fail with "No conversation found" when --resume is used, else echo
  inuse    fail with "already in use" when --session-id is used, else echo
  error    return an is_error result
  garbage  print non-JSON output and exit 1
  sleep    sleep for a long time
Every invocation appends its arguments to FAKE_CLAUDE_LOG (one JSON line) if set.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "echo")
log_path = os.environ.get("FAKE_CLAUDE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "cwd": os.getcwd(), "env_has_api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\n")


def result(text, is_error=False, subtype="success"):
    print(json.dumps({
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "result": text,
        "session_id": "x",
        "total_cost_usd": 0.0123,
        "duration_ms": 42,
    }))


if mode == "sleep":
    time.sleep(60)
elif mode == "garbage":
    print("something went wrong", file=sys.stderr)
    sys.exit(1)
elif mode == "error":
    result("API Error: overloaded", is_error=True)
    sys.exit(1)
elif mode == "missing" and "--resume" in args:
    print("No conversation found with session ID: abc", file=sys.stderr)
    sys.exit(1)
elif mode == "inuse" and "--session-id" in args:
    print("Error: Session ID abc is already in use.", file=sys.stderr)
    sys.exit(1)
else:
    result(f"echo: {prompt}")
