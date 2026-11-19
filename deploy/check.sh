#!/usr/bin/env bash
# Checks the config, the bot tokens and the claude CLI without starting the service.
# Run as root:  sudo bash deploy/check.sh   (the tokens are only readable by root)
set -euo pipefail
set -a
. /etc/claude-assistant/env
set +a
vars=$(grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' /etc/claude-assistant/env | tr -d '=' | paste -sd, -)
export PATH=/home/assistant/.local/bin:/usr/local/bin:/usr/bin:/bin
exec sudo -u assistant -H --preserve-env="$vars,PATH" \
  /home/assistant/app/.venv/bin/claude-assistant --config /home/assistant/config.toml --check
