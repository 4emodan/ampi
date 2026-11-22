#!/usr/bin/env bash
# Installs or updates ampi-tools (Todoist, Gmail, YouTube with approvals) next to the assistant.
# Run as root from a checkout, after deploy/install.sh:  sudo bash deploy/install-tools.sh
#
# The tokens live with a separate `ampitools` user whose home the assistant can't see. The assistant
# gets the `todoist`, `gmail` and `youtube` commands, which send requests to the daemon over a Unix socket.
# Critical requests wait for your Approve/Reject in Telegram.
set -euo pipefail
shopt -s nullglob

TOOLS_USER=ampitools
TOOLS_HOME=/home/$TOOLS_USER
APP_USER=assistant
APP_HOME=/home/$APP_USER
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-tools.sh" >&2
  exit 1
fi

echo "==> User $TOOLS_USER"
getent group "$TOOLS_USER" >/dev/null || groupadd --system "$TOOLS_USER"
if ! id "$TOOLS_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$TOOLS_HOME" --gid "$TOOLS_USER" --shell /usr/sbin/nologin "$TOOLS_USER"
fi
chmod 700 "$TOOLS_HOME"
usermod -aG "$TOOLS_USER" "$APP_USER"   # lets the assistant connect to the socket

echo "==> Code -> $TOOLS_HOME/app"
rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude data \
  --exclude profiles --exclude config.toml --exclude '*.env' --exclude tests \
  "$SRC_DIR"/ "$TOOLS_HOME/app"/
chown -R "$TOOLS_USER:$TOOLS_USER" "$TOOLS_HOME/app"
sudo -u "$TOOLS_USER" -H bash -c "cd ~/app && { test -x .venv/bin/pip || python3 -m venv .venv; } \
  && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q ."

if [[ ! -f $TOOLS_HOME/env ]]; then
  install -o "$TOOLS_USER" -g "$TOOLS_USER" -m 600 "$SRC_DIR/deploy/ampi-tools.env.example" "$TOOLS_HOME/env"
  echo "    created $TOOLS_HOME/env (add TODOIST_TOKEN, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, YOUTUBE_API_KEY)"
fi
if [[ ! -f $TOOLS_HOME/config.toml ]]; then
  # Owned by root: the policy is yours, not something the tools user (or the assistant) can change.
  install -o root -g "$TOOLS_USER" -m 640 "$SRC_DIR/deploy/ampi-tools.config.example.toml" "$TOOLS_HOME/config.toml"
fi

# Telegram writing goes through tg-reader's write socket (group tgwrite, made by install-tg-reader.sh).
if getent group tgwrite >/dev/null; then
  usermod -aG tgwrite "$TOOLS_USER"
fi

echo "==> todoist, gmail, youtube, threads, instagram, telegram, maps, wolt, info, checkout and payment commands for $APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.local/bin"
for cmd in todoist gmail youtube threads instagram telegram maps wolt info checkout payment watchers; do
  ln -sfn "$APP_HOME/app/.venv/bin/$cmd" "$APP_HOME/.local/bin/$cmd"
done

echo "==> Skills and watcher instructions in each profile workspace"
for ws in "$APP_HOME"/profiles/*/; do
  for skill in todoist gmail telegram youtube threads instagram maps wolt personal-info payment watchers; do
    install -d -m 755 "$ws.claude/skills/$skill"
    install -m 644 "$SRC_DIR/templates/skills/$skill/SKILL.md" "$ws.claude/skills/$skill/SKILL.md"
  done
  # Root-owned, so Claude can't rewrite its own skills, settings or hooks.
  chown -R root:root "$ws.claude"
  chmod -R go-w "$ws.claude"
  install -d -o "$APP_USER" -g "$APP_USER" "$ws/watchers"
  for f in "$SRC_DIR"/templates/watchers/*.md; do
    target="$ws/watchers/$(basename "$f")"
    [[ -f $target ]] || install -o "$APP_USER" -g "$APP_USER" -m 644 "$f" "$target"
  done
  # Shared helper scripts for watchers v2: root-owned, so neither Ampi nor the builder can change them (a watcher
  # gets a frozen copy of the ones it uses when it is approved).
  install -d -o root -g root -m 755 "$ws/watchers/lib"
  install -o root -g root -m 644 "$SRC_DIR"/templates/watcher-lib/*.py "$ws/watchers/lib/"
done

echo "==> systemd service"
install -m 644 "$SRC_DIR/deploy/ampi-tools.service" /etc/systemd/system/ampi-tools.service
systemctl daemon-reload
systemctl enable ampi-tools >/dev/null 2>&1 || true
systemctl restart ampi-tools
# The hub (settings pages): its own service on /run/ampi-hub/hub.sock; open it with scripts/ampi-hub.ps1.
install -m 644 "$SRC_DIR/deploy/ampi-hub.service" /etc/systemd/system/ampi-hub.service
install -o root -g root -m 755 "$SRC_DIR/deploy/ampi-hub-token" /usr/local/sbin/ampi-hub-token
systemctl daemon-reload
systemctl enable ampi-hub >/dev/null 2>&1 || true
systemctl restart ampi-hub
# Group membership only applies to new processes.
systemctl is-active --quiet claude-assistant && systemctl restart claude-assistant

cat <<EOF

Done. To connect your accounts (see docs/SETUP.md, section 8):
  1. Todoist token and Gmail OAuth client:   nano $TOOLS_HOME/env
  2. Connect Gmail (you approve in a browser): sudo -u $TOOLS_USER -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools gmail-login'
  3. systemctl restart ampi-tools
  4. Check:  sudo -u $TOOLS_USER -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools check'
  5. YouTube (optional, docs/SETUP.md 8b): YOUTUBE_API_KEY in $TOOLS_HOME/env, and/or
     sudo -u $TOOLS_USER -H bash -c 'set -a; . ~/env; ~/app/.venv/bin/ampi-tools youtube-login'
  6. In $APP_HOME/config.toml: [tools] approvals = true, and add "Bash(todoist:*)", "Bash(gmail:*)",
     "Bash(youtube:*)", "Bash(maps:*)", "Bash(wolt:*)" (8b-2, 8b-3) (and "Bash(telegram:*)" to write in Telegram as you, docs/SETUP.md 7b) to
     allowed_tools; then systemctl restart claude-assistant
EOF
