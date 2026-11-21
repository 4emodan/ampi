#!/usr/bin/env bash
# Installs or updates the checkout browser (used by ampi-tools for purchases the owner approves).
# Run as root from a checkout, after deploy/install-browser.sh (it installs the Obscura engine) and
# deploy/install-tools.sh:  sudo bash deploy/install-checkout.sh
#
# Three pieces, three users:
#   ampicheckout     runs the engine (obscura serve on 127.0.0.1:19333), internet 80/443 only, no local addresses
#   ampicheckoutctl  runs the controller, no internet, the only user allowed to reach the engine's port
#   ampitools        (existing) the only member of group ampicheckoutctl-use, i.e. the only one who may use
#                    /run/ampi-checkout.sock
set -euo pipefail

ENGINE_USER=ampicheckout
CTL_USER=ampicheckoutctl
USE_GROUP=ampicheckoutctl-use
TOOLS_USER=ampitools
LIB_DIR=/usr/local/lib/ampi-checkout
CONF_DIR=/etc/ampi-checkout
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-checkout.sh" >&2
  exit 1
fi
if [[ ! -x /usr/local/lib/obscura/current/obscura ]]; then
  echo "The Obscura engine is missing: run deploy/install-browser.sh first" >&2
  exit 1
fi
if ! id "$TOOLS_USER" >/dev/null 2>&1; then
  echo "User $TOOLS_USER is missing: run deploy/install-tools.sh first" >&2
  exit 1
fi

echo "==> Users: $ENGINE_USER (engine), $CTL_USER (controller), group $USE_GROUP ($TOOLS_USER only)"
for u in "$ENGINE_USER" "$CTL_USER"; do
  getent group "$u" >/dev/null || groupadd --system "$u"
  id "$u" >/dev/null 2>&1 || useradd --system --gid "$u" --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$u"
done
getent group "$USE_GROUP" >/dev/null || groupadd --system "$USE_GROUP"
usermod -aG "$USE_GROUP" "$TOOLS_USER"

echo "==> Code and config"
install -d -o root -g root -m 755 "$LIB_DIR" "$CONF_DIR"
for f in checkout.py checkout_net.py browser.py browser_net.py; do
  install -o root -g root -m 644 "$SRC_DIR/src/ampi_tools/$f" "$LIB_DIR/$f"
done
if [[ ! -f $CONF_DIR/resolvers ]]; then
  if [[ -f /etc/ampi-browser/resolvers ]]; then
    install -m 644 /etc/ampi-browser/resolvers "$CONF_DIR/resolvers"
  else
    printf '%s\n' "# Public DNS resolvers the checkout browser may use (Hetzner's)." \
      185.12.64.1 185.12.64.2 2a01:4ff:ff00::add:1 2a01:4ff:ff00::add:2 > "$CONF_DIR/resolvers"
  fi
fi
printf 'AMPI_CHECKOUT_CLIENT_UIDS=%s\nAMPI_CHECKOUT_ENGINE_UID=%s\n' "$(id -u "$TOOLS_USER")" "$(id -u "$ENGINE_USER")" \
  > "$CONF_DIR/ctl.env"
chmod 644 "$CONF_DIR/resolvers" "$CONF_DIR/ctl.env"

echo "==> systemd units"
for unit in ampi-checkout-net.service ampi-checkout-engine.service ampi-checkout.service ampi-checkout.socket; do
  install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable ampi-checkout-net.service ampi-checkout-engine.service ampi-checkout.socket >/dev/null 2>&1
systemctl restart ampi-checkout-net.service
systemctl restart ampi-checkout-engine.service
systemctl stop ampi-checkout.service 2>/dev/null || true
systemctl restart ampi-checkout.socket

echo "==> Check"
sleep 2
systemctl is-active --quiet ampi-checkout-engine.service || {
  echo "The engine didn't start; see: journalctl -u ampi-checkout-engine -n 30" >&2; exit 1; }
# As ampitools, through the real socket (also starts the controller).
if ! out=$(sudo -u "$TOOLS_USER" /usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/ampi-checkout.sock")
s.sendall(b"{\"cmd\": \"status\"}\n"); print(s.recv(4096).decode().strip())' 2>&1) || [[ $out != *'"ok": true'* ]]; then
  echo "The controller didn't answer: $out (journalctl -u ampi-checkout -n 30)" >&2
  exit 1
fi
echo "    controller: $out"
# Nobody else may reach the engine's port: try as the assistant's user (expect a refusal).
if id assistant >/dev/null 2>&1; then
  if sudo -u assistant /usr/bin/python3 -I -c '
import socket; socket.create_connection(("127.0.0.1", 19333), timeout=3)' 2>/dev/null; then
    echo "SECURITY: user assistant could connect to the engine's port; check the nft table" >&2
    exit 1
  fi
  echo "    engine port refused for other users: ok"
fi
# Group membership only applies to new processes.
systemctl is-active --quiet ampi-tools && systemctl restart ampi-tools

echo "Done."
