#!/usr/bin/env bash
# Installs or updates the gateway for the owner's phone (companion app, docs/COMPANION.md).
# Run as root from a checkout, after deploy/install-tools.sh:  sudo bash deploy/install-phone.sh
# Removing everything again:                                   sudo bash deploy/install-phone.sh --uninstall
#
# Nothing listens on the network until a phone is paired (scripts/companion.ps1 -Pair on your PC).
set -euo pipefail

GW_USER=ampiphone
USE_GROUP=ampiphone-use
TOOLS_USER=ampitools
PORT=28471
LIB_DIR=/usr/local/lib/ampi-phone
CONF_DIR=/etc/ampi-phone
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-phone.sh" >&2
  exit 1
fi

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now ampi-phone.socket ampi-phone.service 2>/dev/null || true
  command -v ufw >/dev/null && ufw --force delete allow "$PORT/tcp" >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ampi-phone.socket /etc/systemd/system/ampi-phone.service /usr/local/sbin/ampi-phone
  systemctl daemon-reload
  rm -rf "$LIB_DIR" "$CONF_DIR"
  userdel "$GW_USER" 2>/dev/null || true
  gpasswd -d "$TOOLS_USER" "$USE_GROUP" >/dev/null 2>&1 || true
  groupdel "$USE_GROUP" 2>/dev/null || true
  echo "Removed. The phone must be paired again after a new install."
  exit 0
fi
if ! id "$TOOLS_USER" >/dev/null 2>&1; then
  echo "User $TOOLS_USER is missing: run deploy/install-tools.sh first" >&2
  exit 1
fi

echo "==> User $GW_USER, group $USE_GROUP ($TOOLS_USER only)"
getent group "$GW_USER" >/dev/null || groupadd --system "$GW_USER"
id "$GW_USER" >/dev/null 2>&1 || useradd --system --gid "$GW_USER" --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin "$GW_USER"
getent group "$USE_GROUP" >/dev/null || groupadd --system "$USE_GROUP"
new_member=0
id -nG "$TOOLS_USER" | tr ' ' '\n' | grep -qx "$USE_GROUP" || { usermod -aG "$USE_GROUP" "$TOOLS_USER"; new_member=1; }

echo "==> Code and config"
install -d -o root -g root -m 755 "$LIB_DIR" "$LIB_DIR/ampi_phone"
for f in __init__.py trust.py gateway.py admin.py; do
  install -o root -g root -m 644 "$SRC_DIR/src/ampi_phone/$f" "$LIB_DIR/ampi_phone/$f"
done
install -o root -g root -m 644 "$SRC_DIR/deploy/ampi-phone-run.py" "$LIB_DIR/run.py"
install -o root -g root -m 755 "$SRC_DIR/deploy/ampi-phone" /usr/local/sbin/ampi-phone
# Only root writes here: the gateway can't change who is paired.
install -d -o root -g "$GW_USER" -m 750 "$CONF_DIR" "$CONF_DIR/devices"
if [[ ! -f $CONF_DIR/server.key || ! -f $CONF_DIR/server.crt ]]; then
  if compgen -G "$CONF_DIR/devices/*.json" >/dev/null; then
    echo "the server key or certificate is missing but phones are paired: restore it, or remove them first" >&2; exit 1
  fi
  (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 9000 -sha256 \
    -subj /CN=ampi-phone -keyout "$CONF_DIR/server.key" -out "$CONF_DIR/server.crt")
fi
chown root:"$GW_USER" "$CONF_DIR/server.key" "$CONF_DIR/server.crt"
chmod 640 "$CONF_DIR/server.key" "$CONF_DIR/server.crt"
if [[ ! -f $CONF_DIR/config.json ]]; then
  host=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  [[ $host =~ ^[0-9.]+$ ]] || { echo "can't tell this server's address; write $CONF_DIR/config.json yourself" >&2; exit 1; }
  printf '{"host": "%s", "port": %s}\n' "$host" "$PORT" > "$CONF_DIR/config.json"
fi
chmod 644 "$CONF_DIR/config.json"
printf 'AMPI_PHONE_CLIENT_UIDS=%s\n' "$(id -u "$TOOLS_USER")" > "$CONF_DIR/gateway.env"

echo "==> systemd units"
# The socket is only restarted when its unit changed: while it listens, nobody else can take the port.
socket_changed=0
cmp -s "$SRC_DIR/deploy/ampi-phone.socket" /etc/systemd/system/ampi-phone.socket || socket_changed=1
install -m 644 "$SRC_DIR/deploy/ampi-phone.socket" /etc/systemd/system/ampi-phone.socket
install -m 644 "$SRC_DIR/deploy/ampi-phone.service" /etc/systemd/system/ampi-phone.service
systemctl daemon-reload
if compgen -G "$CONF_DIR/devices/*.json" >/dev/null; then
  systemctl enable ampi-phone.socket >/dev/null 2>&1
  if [[ $socket_changed == 1 ]]; then
    systemctl stop ampi-phone.service ampi-phone.socket 2>/dev/null || true
  fi
  systemctl start ampi-phone.socket
  systemctl restart ampi-phone.service
  sleep 1
  systemctl is-active --quiet ampi-phone.service || { echo "the gateway didn't start: journalctl -u ampi-phone -n 30" >&2; exit 1; }
  echo "    running: $(ls "$CONF_DIR/devices" | wc -l) paired phone(s)"
else
  echo "    no phone paired yet: nothing listens. On your PC: scripts/companion.ps1 -Install -Pair"
fi
# Group membership only applies to new processes.
if [[ $new_member == 1 ]] && systemctl is-active --quiet ampi-tools; then systemctl restart ampi-tools; fi
echo "Done."