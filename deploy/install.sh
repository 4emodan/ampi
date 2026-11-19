#!/usr/bin/env bash
# Installs or updates the Claude Assistant bridge on a Debian/Ubuntu server.
# Run as root from a checkout of this repository:  sudo bash deploy/install.sh
# Safe to run again for updates; it never overwrites config.toml, the env file or workspaces.
set -euo pipefail

APP_USER=assistant
HOME_DIR=/home/$APP_USER
APP_DIR=$HOME_DIR/app
ENV_DIR=$HOME_DIR/.config/claude-assistant
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> Packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl rsync ca-certificates >/dev/null

echo "==> User $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$HOME_DIR" --shell /bin/bash "$APP_USER"
fi
chmod 700 "$HOME_DIR"

echo "==> uv (Python package manager) for $APP_USER"
if ! sudo -u "$APP_USER" -H bash -lc 'command -v uv >/dev/null || test -x ~/.local/bin/uv'; then
  sudo -u "$APP_USER" -H bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

echo "==> App code -> $APP_DIR"
rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude data --exclude profiles \
  --exclude config.toml --exclude '*.env' \
  "$SRC_DIR"/ "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
sudo -u "$APP_USER" -H bash -lc "cd '$APP_DIR' && ~/.local/bin/uv sync --no-dev -q"
# Read-only for the assistant from here on: Claude must not be able to change the code it runs under.
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R go-w "$APP_DIR"

echo "==> Config"
if [[ ! -f $HOME_DIR/config.toml ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 600 "$APP_DIR/config.example.toml" "$HOME_DIR/config.toml"
  echo "    created $HOME_DIR/config.toml (edit it)"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$ENV_DIR"
if [[ ! -f $ENV_DIR/env ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$ENV_DIR/env"
  echo "# Telegram bot tokens, one per profile (names must match bot_token_env in config.toml)" >"$ENV_DIR/env"
  echo "TG_TOKEN_PERSONAL=" >>"$ENV_DIR/env"
  echo "    created $ENV_DIR/env (add your bot tokens)"
fi
if [[ ! -f $HOME_DIR/profiles/personal/CLAUDE.md ]]; then
  install -d -o "$APP_USER" -g "$APP_USER" "$HOME_DIR/profiles/personal"
  install -o "$APP_USER" -g "$APP_USER" -m 644 "$APP_DIR/templates/profile/CLAUDE.md" "$HOME_DIR/profiles/personal/CLAUDE.md"
  echo "    created $HOME_DIR/profiles/personal/CLAUDE.md (persona for the 'personal' profile)"
fi

echo "==> systemd service"
install -m 644 "$APP_DIR/deploy/claude-assistant.service" /etc/systemd/system/claude-assistant.service
systemctl daemon-reload
systemctl enable claude-assistant >/dev/null 2>&1 || true

if systemctl is-active --quiet claude-assistant; then
  systemctl restart claude-assistant
  echo "==> Restarted the running service"
fi

cat <<EOF

Done. If this is the first install:
  1. Install Claude Code for the $APP_USER user and sign in with your Claude account:
       sudo -iu $APP_USER
       curl -fsSL https://claude.ai/install.sh | bash
       claude            # follow the login link, then type /exit
  2. Add bot tokens:     $ENV_DIR/env
  3. Edit the config:    $HOME_DIR/config.toml   (owner_ids = your Telegram user ID)
  4. Check everything:   sudo -iu $APP_USER bash -c 'set -a; . $ENV_DIR/env; $APP_DIR/.venv/bin/claude-assistant --config ~/config.toml --check'
  5. Start it:           systemctl start claude-assistant && journalctl -u claude-assistant -f
EOF
