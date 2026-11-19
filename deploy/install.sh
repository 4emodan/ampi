#!/usr/bin/env bash
# Installs or updates the Claude Assistant bridge on a Debian/Ubuntu server.
# Run as root from a checkout of this repository:  sudo bash deploy/install.sh
# Safe to run again for updates; it never overwrites config.toml, the env file or workspaces.
set -euo pipefail
shopt -s nullglob

APP_USER=assistant
HOME_DIR=/home/$APP_USER
APP_DIR=$HOME_DIR/app
OLD_ENV=$HOME_DIR/.config/claude-assistant/env  # where the bot tokens used to live
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


# Commands run as the assistant user never read its shell startup files (Claude could have edited them).
as_app() { sudo -u "$APP_USER" -H env -i HOME="$HOME_DIR" PATH="$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin" \
  bash --noprofile --norc -c "$1"; }

echo "==> uv (Python package manager) for $APP_USER"
if ! as_app 'test -x ~/.local/bin/uv'; then
  as_app 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

echo "==> App code -> $APP_DIR"
# Stop first: while the code is briefly owned by the assistant user for `uv sync`, nothing may run it.
was_active=0
systemctl is-active --quiet claude-assistant && was_active=1 && systemctl stop claude-assistant
rsync -a --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude data --exclude profiles \
  --exclude config.toml --exclude '*.env' \
  "$SRC_DIR"/ "$APP_DIR"/
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
# Whatever happens next, never leave the code owned by the assistant user.
trap 'chown -R root:"$APP_USER" "$APP_DIR"; chmod -R u+rwX,g+rX,g-w,o-rwx "$APP_DIR"' EXIT
as_app "cd '$APP_DIR' && UV_PYTHON_PREFERENCE=only-system ~/.local/bin/uv sync --no-dev -q --python /usr/bin/python3"
# Read-only for the assistant from here on: Claude must not be able to change the code it runs under.
chown -R root:"$APP_USER" "$APP_DIR"
chmod -R u+rwX,g+rX,g-w,o-rwx "$APP_DIR"

echo "==> Secrets and config (root-owned: the assistant can read the config but change neither)"
install -d -m 700 /etc/claude-assistant
if [[ ! -f /etc/claude-assistant/env ]]; then
  if [[ -f $OLD_ENV ]]; then
    install -m 600 "$OLD_ENV" /etc/claude-assistant/env   # moved from the assistant's home
    rm -f "$OLD_ENV"
    echo "    moved $OLD_ENV to /etc/claude-assistant/env"
  else
    install -m 600 /dev/null /etc/claude-assistant/env
    echo "# Telegram bot tokens, one per profile (names must match bot_token_env in config.toml)" >/etc/claude-assistant/env
    echo "TG_TOKEN_PERSONAL=" >>/etc/claude-assistant/env
    echo "    created /etc/claude-assistant/env (add your bot tokens)"
  fi
fi
chown root:root /etc/claude-assistant/env && chmod 600 /etc/claude-assistant/env
if [[ ! -f $HOME_DIR/config.toml ]]; then
  install -m 640 "$APP_DIR/config.example.toml" "$HOME_DIR/config.toml"
  echo "    created $HOME_DIR/config.toml (edit it)"
fi
chown root:"$APP_USER" "$HOME_DIR"/config.toml* && chmod 640 "$HOME_DIR"/config.toml*

echo "==> Home directory lockdown"
# No SSH logins as the assistant, and startup files it can't rewrite.
install -d -o root -g root -m 755 "$HOME_DIR/.ssh"
rm -f "$HOME_DIR/.ssh/authorized_keys"
for f in .bashrc .profile .bash_logout .zshrc .bash_profile; do
  [[ -e $HOME_DIR/$f ]] && chown root:root "$HOME_DIR/$f" && chmod 644 "$HOME_DIR/$f"
done
install -d -o "$APP_USER" -g "$APP_USER" "$HOME_DIR/data"

if [[ ! -f $HOME_DIR/profiles/personal/CLAUDE.md ]]; then
  install -d -o "$APP_USER" -g "$APP_USER" "$HOME_DIR/profiles/personal"
  install -m 644 "$APP_DIR/templates/profile/CLAUDE.md" "$HOME_DIR/profiles/personal/CLAUDE.md"
  echo "    created $HOME_DIR/profiles/personal/CLAUDE.md (persona for the 'personal' profile)"
fi
# The persona is yours: root-owned, so injected text can't rewrite it for every later run.
for ws in "$HOME_DIR"/profiles/*/; do
  [[ -f ${ws}CLAUDE.md ]] && chown root:"$APP_USER" "${ws}CLAUDE.md" && chmod 644 "${ws}CLAUDE.md"
  install -d -m 755 "${ws}.claude" && chown root:root "${ws}.claude"
done

echo "==> systemd service"
install -m 644 "$APP_DIR/deploy/claude-assistant.service" /etc/systemd/system/claude-assistant.service
systemctl daemon-reload
systemctl enable claude-assistant >/dev/null 2>&1 || true
if [[ $was_active == 1 ]]; then
  systemctl start claude-assistant
  echo "==> Restarted the service"
fi

cat <<EOF

Done. If this is the first install:
  1. Install Claude Code for the $APP_USER user and sign in with your Claude account:
       sudo -iu $APP_USER
       curl -fsSL https://claude.ai/install.sh | bash
       claude            # follow the login link, then type /exit
  2. Add bot tokens:     /etc/claude-assistant/env
  3. Edit the config:    $HOME_DIR/config.toml   (owner_ids = your Telegram user ID)
  4. Check everything:   sudo bash $APP_DIR/deploy/check.sh
  5. Start it:           systemctl start claude-assistant && journalctl -u claude-assistant -f
EOF
