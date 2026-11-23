#!/usr/bin/env bash
# Installs or updates the checkout browser (used by ampi-tools for purchases the owner approves).
# Run as root from a checkout, after deploy/install-browser.sh (it installs the Obscura engine) and
# deploy/install-tools.sh:  sudo bash deploy/install-checkout.sh
#
# Three pieces, three users:
#   ampicheckout     runs the engine (obscura serve on 127.0.0.1:19333), internet 80/443 only, no local addresses
#   ampicheckoutctl  runs the controller, no internet, the only user allowed to reach the engine's port
#   ampitools        (existing) the only member of group ampicheckoutctl-use, i.e. the only one who may use
#                    /run/ampi-checkout.sock (and ampi-signin, whose unit gets the group: only to fill a login in)
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

. "$SRC_DIR/deploy/checkout-busy.sh"
if checkout_busy; then
  echo "A checkout session is open; close it (or wait 10 minutes) and run this again." >&2
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
# The sign-in service (deploy/install-phone.sh) may ask the controller to fill a login in, and for nothing else.
if [[ -x /usr/local/lib/lightpanda/current/lightpanda ]] && id ampicheckoutlp >/dev/null 2>&1; then
  printf 'AMPI_CHECKOUT_LP_UID=%s\n' "$(id -u ampicheckoutlp)" >> "$CONF_DIR/ctl.env"
fi
if id ampisignin >/dev/null 2>&1; then printf 'AMPI_CHECKOUT_SIGNIN_UIDS=%s\n' "$(id -u ampisignin)" >> "$CONF_DIR/ctl.env"; fi
chmod 644 "$CONF_DIR/resolvers" "$CONF_DIR/ctl.env"

echo "==> systemd units"
for unit in ampi-checkout-net.service ampi-checkout-engine.service ampi-checkout.service ampi-checkout.socket; do
  install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
# The engine has no [Install]: it runs only together with the controller (BindsTo=), which the socket starts.
systemctl disable ampi-checkout-engine.service >/dev/null 2>&1 || true
systemctl enable ampi-checkout-net.service ampi-checkout.socket >/dev/null 2>&1
systemctl stop ampi-checkout.service ampi-checkout-engine.service 2>/dev/null || true
systemctl restart ampi-checkout-net.service
systemctl restart ampi-checkout.socket
systemctl stop ampi-signin.service 2>/dev/null || true  # (it sees the socket as it was when it started)

echo "==> Check"
# As ampitools, through the real socket: starts the controller, and with it the engine.
if ! out=$(sudo -u "$TOOLS_USER" /usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(10); s.connect("/run/ampi-checkout.sock")
s.sendall(b"{\"cmd\": \"status\"}\n"); print(s.recv(4096).decode().strip())' 2>&1) || [[ $out != *'"ok": true'* ]]; then
  echo "The controller didn't answer: $out (journalctl -u ampi-checkout -n 30)" >&2
  exit 1
fi
echo "    controller: $out"
for _ in $(seq 1 30); do
  ss -Hltn 'sport = :19333' | grep -q . && break
  sleep 0.5
done
systemctl is-active --quiet ampi-checkout-engine.service || {
  echo "The engine didn't start with the controller; see: journalctl -u ampi-checkout-engine -n 30" >&2; exit 1; }
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
