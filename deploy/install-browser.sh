#!/usr/bin/env bash
# Installs or updates the sandboxed web browser (`browse` command, Obscura engine) for Claude Assistant.
# Run as root from a checkout, after deploy/install.sh:  sudo bash deploy/install-browser.sh
#
# The engine is a prebuilt release pinned by sha256. That proves the file is the one published on the
# release page, not who built it: it is a young third-party project, which is why it runs in a tight
# sandbox (deploy/ampi-browser@.service) behind its own egress firewall (src/ampi_tools/browser_net.py).
set -euo pipefail

VERSION=0.2.7
ASSET=obscura-x86_64-linux-no-render.tar.gz
SHA256=d64c8521b95bb7cd0efb479b29c88bb5cd297949f6ccf2b7cca82f40e8d26948
URL=https://github.com/h4ckf0r0day/obscura/releases/download/v$VERSION/$ASSET
ENGINE_DIR=/usr/local/lib/obscura
LIB_DIR=/usr/local/lib/ampi-browser
CONF_DIR=/etc/ampi-browser
BROWSER_USER=ampibrowser
SOCKET_GROUP=ampibrowse
APP_USER=assistant
APP_HOME=/home/$APP_USER
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-browser.sh" >&2
  exit 1
fi
if [[ $(uname -m) != x86_64 ]]; then
  echo "Only x86_64 is set up (edit ASSET/SHA256 for another architecture)" >&2
  exit 1
fi

echo "==> Users: $BROWSER_USER (runs the engine), group $SOCKET_GROUP (may use the socket)"
getent group "$BROWSER_USER" >/dev/null || groupadd --system "$BROWSER_USER"
id "$BROWSER_USER" >/dev/null 2>&1 || useradd --system --gid "$BROWSER_USER" --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin "$BROWSER_USER"
getent group "$SOCKET_GROUP" >/dev/null || groupadd --system "$SOCKET_GROUP"
usermod -aG "$SOCKET_GROUP" "$APP_USER"

echo "==> Engine: Obscura $VERSION ($ASSET)"
target=$ENGINE_DIR/$VERSION
if [[ ! -x $target/obscura ]]; then
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  curl -fsSL --proto '=https' --tlsv1.2 --max-filesize 100000000 -o "$work/$ASSET" "$URL"
  echo "$SHA256  $work/$ASSET" | sha256sum -c --quiet -
  # Only the files we expect, as plain relative paths (the archive may or may not have a top folder).
  members=$(tar -tzf "$work/$ASSET")
  if grep -Eq '(^/|(^|/)\.\.(/|$))' <<<"$members"; then
    echo "archive has absolute or .. paths; refusing" >&2; exit 1
  fi
  if tar -tvzf "$work/$ASSET" | grep -Ev '^[-d]' | grep -q .; then
    echo "archive has links or special files; refusing" >&2; exit 1
  fi
  engine=$(grep -E '(^|/)obscura$' <<<"$members" || true)
  if [[ $(wc -l <<<"$engine") -ne 1 || -z $engine ]]; then
    echo "expected exactly one 'obscura' binary in the archive, got: $members" >&2; exit 1
  fi
  mkdir "$work/x"
  tar -xzf "$work/$ASSET" -C "$work/x" --no-same-owner --no-same-permissions "$engine"
  install -d -o root -g "$BROWSER_USER" -m 750 "$ENGINE_DIR" "$target.new"
  install -o root -g "$BROWSER_USER" -m 750 "$work/x/$engine" "$target.new/obscura"
  rm -rf "$target"
  mv "$target.new" "$target"
fi
previous=$(readlink "$ENGINE_DIR/current" 2>/dev/null || true)

echo "==> Service code and config"
install -d -o root -g root -m 755 "$LIB_DIR" "$CONF_DIR"
install -o root -g root -m 644 "$SRC_DIR/src/ampi_tools/browser.py" "$LIB_DIR/browser.py"
install -o root -g root -m 644 "$SRC_DIR/src/ampi_tools/browser_net.py" "$LIB_DIR/browser_net.py"
[[ -f $CONF_DIR/allow-origins ]] || printf '%s\n' \
  "# Runs that handle outside content may browse only if listed here, e.g. group:clipboard (one per line)." \
  > "$CONF_DIR/allow-origins"
[[ -f $CONF_DIR/resolvers ]] || printf '%s\n' "# Public DNS resolvers the browser may use (Hetzner's)." \
  185.12.64.1 185.12.64.2 2a01:4ff:ff00::add:1 2a01:4ff:ff00::add:2 > "$CONF_DIR/resolvers"
echo "AMPI_BROWSER_CLIENT_UIDS=$(id -u "$APP_USER")" > "$CONF_DIR/instance.env"
chmod 644 "$CONF_DIR"/allow-origins "$CONF_DIR"/resolvers "$CONF_DIR"/instance.env

echo "==> systemd units"
for unit in ampi-browser-net.service ampi-browser.socket ampi-browser@.service; do
  install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable ampi-browser-net.service ampi-browser.socket >/dev/null 2>&1
systemctl restart ampi-browser-net.service
ln -sfn "$target" "$ENGINE_DIR/current.tmp" && mv -T "$ENGINE_DIR/current.tmp" "$ENGINE_DIR/current"
systemctl restart ampi-browser.socket

echo "==> browse command and skill for $APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.local/bin"
ln -sfn "$APP_HOME/app/.venv/bin/browse" "$APP_HOME/.local/bin/browse"
for ws in "$APP_HOME"/profiles/*/; do
  install -d -m 755 "$ws.claude/skills/browse"
  install -m 644 "$SRC_DIR/templates/skills/browse/SKILL.md" "$ws.claude/skills/browse/SKILL.md"
  chown -R root:root "$ws.claude"
  chmod -R go-w "$ws.claude"
done

echo "==> Check through the real sandbox"
if ! status=$(sudo -u "$APP_USER" -H "$APP_HOME/app/.venv/bin/browse" status 2>&1) \
    || [[ $status == *"not working"* ]]; then
  echo "$status" >&2
  if [[ -n $previous && $previous != "$target" ]]; then
    ln -sfn "$previous" "$ENGINE_DIR/current.tmp" && mv -T "$ENGINE_DIR/current.tmp" "$ENGINE_DIR/current"
    echo "The new engine didn't start in the sandbox; switched back to $previous" >&2
  else
    echo "The engine didn't start in the sandbox; see: journalctl -u 'ampi-browser@*' -n 50" >&2
  fi
  exit 1
fi
echo "    $status"
# Group membership only applies to new processes.
systemctl is-active --quiet claude-assistant && systemctl restart claude-assistant

cat <<EOF

Done. To let Ampi use it, add "Bash(browse:*)" to allowed_tools in $APP_HOME/config.toml and
systemctl restart claude-assistant. Watchers and groups never get it (see docs/SETUP.md, section 8d).
EOF
