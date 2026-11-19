#!/usr/bin/env bash
# Installs or updates the read-only Telegram reader (tg-reader) next to the assistant.
# Run as root from a checkout, after deploy/install.sh:  sudo bash deploy/install-tg-reader.sh
#
# Why a separate user: the reader logs in to Telegram as you, and that session could send or delete
# anything. It lives in /home/tgreader, which the assistant can't see. The assistant only gets the
# `tg-read` command, which talks to the reader over a Unix socket that answers read-only requests.
set -euo pipefail

READER_USER=tgreader
READER_GROUP=tgread
READER_HOME=/home/$READER_USER
APP_USER=assistant
APP_HOME=/home/$APP_USER
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-tg-reader.sh" >&2
  exit 1
fi

echo "==> Group $READER_GROUP and user $READER_USER"
getent group "$READER_GROUP" >/dev/null || groupadd --system "$READER_GROUP"
if ! id "$READER_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$READER_HOME" --gid "$READER_GROUP" --shell /usr/sbin/nologin "$READER_USER"
fi
chmod 700 "$READER_HOME"
usermod -aG "$READER_GROUP" "$APP_USER"   # lets the assistant connect to the socket

echo "==> Reader code -> $READER_HOME/app (with Telethon)"
rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude data \
  --exclude profiles --exclude config.toml --exclude '*.env' --exclude tests \
  "$SRC_DIR"/ "$READER_HOME/app"/
chown -R "$READER_USER:$READER_GROUP" "$READER_HOME/app"
sudo -u "$READER_USER" -H bash -c "cd ~/app && { test -x .venv/bin/pip || python3 -m venv .venv; } \
  && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q '.[reader]'"

if [[ ! -f $READER_HOME/env ]]; then
  install -o "$READER_USER" -g "$READER_GROUP" -m 600 "$SRC_DIR/deploy/tg-reader.env.example" "$READER_HOME/env"
  echo "    created $READER_HOME/env (add TG_API_ID and TG_API_HASH)"
fi

echo "==> tg-read command for $APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.local/bin"
ln -sfn "$APP_HOME/app/.venv/bin/tg-read" "$APP_HOME/.local/bin/tg-read"

echo "==> telegram skill in each profile workspace"
for ws in "$APP_HOME"/profiles/*/; do
  install -d -o "$APP_USER" -g "$APP_USER" "$ws.claude" "$ws.claude/skills" "$ws.claude/skills/telegram"
  install -o "$APP_USER" -g "$APP_USER" -m 644 "$SRC_DIR/templates/skills/telegram/SKILL.md" \
    "$ws.claude/skills/telegram/SKILL.md"
done

echo "==> systemd service"
install -m 644 "$SRC_DIR/deploy/tg-reader.service" /etc/systemd/system/tg-reader.service
systemctl daemon-reload
systemctl enable tg-reader >/dev/null 2>&1 || true
if [[ -f $READER_HOME/tg-reader.session ]]; then
  systemctl restart tg-reader
  echo "==> Restarted tg-reader"
fi
# Group membership only applies to new processes.
systemctl is-active --quiet claude-assistant && systemctl restart claude-assistant

cat <<EOF

Done. If this is the first install:
  1. Get an API id and hash at https://my.telegram.org -> API development tools,
     and put them in $READER_HOME/env  (nano $READER_HOME/env)
  2. Log in (Telegram sends a code to your app; type it here):
       sudo -u $READER_USER -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/tg-reader login'
  3. Start it:   systemctl restart tg-reader && journalctl -u tg-reader -n 20
  4. Let the assistant use it: in $APP_HOME/config.toml add "Skill" and "Bash(tg-read:*)"
     to allowed_tools, then systemctl restart claude-assistant
To revoke access at any time: Telegram -> Settings -> Devices -> "Ampi reader (read-only)" -> Terminate.
EOF
