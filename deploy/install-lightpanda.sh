#!/usr/bin/env bash
# Installs or updates the checkout browser's second engine (Lightpanda), for sites the first can't run.
# Run as root from a checkout, after deploy/install-checkout.sh:  sudo bash deploy/install-lightpanda.sh
# Removing it again:                                             sudo bash deploy/install-lightpanda.sh --uninstall
#
# One prebuilt file, pinned by sha256. That proves it is the one published on the release page, not who built it: it runs
# as its own user behind the same firewall and sandbox as the first engine, never sees card details, and is thrown away
# after every session (deploy/ampi-checkout-lp.service, src/ampi_tools/checkout_net.py).
set -euo pipefail

VERSION=1.0.5
ASSET=lightpanda-x86_64-linux
SHA256=0ec21fa4867ab9e82b35285ff4ab0e7336c6c1d89f26fe991f27d73c8a678bd7
URL=https://github.com/lightpanda-io/browser/releases/download/$VERSION/$ASSET
LP_USER=ampicheckoutlp
DIR=/usr/local/lib/lightpanda
CTL_ENV=/etc/ampi-checkout/ctl.env
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-lightpanda.sh" >&2
  exit 1
fi
. "$SRC_DIR/deploy/checkout-busy.sh"
if checkout_busy; then  # an open checkout session (perhaps paying) must not be cut off
  echo "A checkout session is open; close it (or wait 10 minutes) and run this again." >&2
  exit 1
fi

if [[ ${1:-} == --uninstall ]]; then
  systemctl stop ampi-checkout.service ampi-checkout-lp.service 2>/dev/null || true
  rm -f /etc/systemd/system/ampi-checkout-lp.service
  systemctl daemon-reload
  [[ -f $CTL_ENV ]] && sed -i '/^AMPI_CHECKOUT_LP_UID=/d' "$CTL_ENV"
  rm -rf "$DIR" /etc/ampi-checkout/passwd-lp /etc/ampi-checkout/group-lp
  userdel "$LP_USER" 2>/dev/null || true
  systemctl restart ampi-checkout-net.service
  echo "Removed."
  exit 0
fi
if [[ $(uname -m) != x86_64 ]]; then
  echo "Only x86_64 is set up (edit ASSET/SHA256 for another architecture)" >&2
  exit 1
fi
if [[ ! -f $CTL_ENV ]]; then
  echo "the checkout browser isn't installed: run deploy/install-checkout.sh first" >&2
  exit 1
fi

echo "==> User $LP_USER"
getent group "$LP_USER" >/dev/null || groupadd --system "$LP_USER"
id "$LP_USER" >/dev/null 2>&1 || useradd --system --gid "$LP_USER" --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin "$LP_USER"

echo "==> Engine: Lightpanda $VERSION"
target=$DIR/$VERSION
if [[ ! -x $target/lightpanda ]]; then
  work=$(mktemp -d)
  trap 'rm -rf "$work"' EXIT
  curl -fsSL --proto '=https' --tlsv1.2 --max-filesize 120000000 -o "$work/$ASSET" "$URL"
  echo "$SHA256  $work/$ASSET" | sha256sum -c --quiet -
  install -d -o root -g "$LP_USER" -m 750 "$DIR" "$target.new"
  install -o root -g "$LP_USER" -m 750 "$work/$ASSET" "$target.new/lightpanda"
  rm -rf "$target"
  mv "$target.new" "$target"
fi
ln -sfn "$VERSION" "$DIR/current"

echo "==> Firewall, unit, controller"
install -m 644 "$SRC_DIR/src/ampi_tools/checkout_net.py" /usr/local/lib/ampi-checkout/checkout_net.py
install -m 644 "$SRC_DIR/src/ampi_tools/checkout.py" /usr/local/lib/ampi-checkout/checkout.py
for unit in ampi-checkout.service ampi-checkout-lp.service; do
  install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
sed -i '/^AMPI_CHECKOUT_LP_UID=/d' "$CTL_ENV"
printf 'AMPI_CHECKOUT_LP_UID=%s\n' "$(id -u "$LP_USER")" >> "$CTL_ENV"
systemctl daemon-reload
systemctl stop ampi-checkout.service ampi-checkout-engine.service ampi-checkout-lp.service 2>/dev/null || true
systemctl restart ampi-checkout-net.service

# Until it has shown that it works, it isn't offered.
give_up() {
  echo "$1" >&2
  sed -i '/^AMPI_CHECKOUT_LP_UID=/d' "$CTL_ENV"
  systemctl stop ampi-checkout.service ampi-checkout-lp.service 2>/dev/null || true
  # (Neither the controller's Wants= nor a later install-checkout.sh may bring it back.)
  rm -f "$DIR/current" /etc/systemd/system/ampi-checkout-lp.service
  systemctl daemon-reload
  exit 1
}
ask() {  # one request to the controller, as ampi-tools
  sudo -u ampitools /usr/bin/python3 -I -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(90); s.connect("/run/ampi-checkout.sock")
s.sendall(sys.argv[1].encode() + b"\n"); r = json.loads(s.makefile().readline())
v = r
for k in sys.argv[2].split("."): v = (v or {}).get(k)
print(v if r.get("ok") else "error: " + str(r.get("error")))' "$@"
}

echo "==> Check"
ask '{"cmd": "status"}' state >/dev/null || give_up "the checkout browser doesn't answer"  # (starts it, with both engines)
for _ in $(seq 1 30); do
  ss -Hltn 'sport = :19334' | grep -q . && break
  sleep 0.5
done
systemctl is-active --quiet ampi-checkout-lp.service && ss -Hltn 'sport = :19334' | grep -q . || {
  give_up "The engine didn't start; see: systemctl status ampi-checkout-lp"; }
if [[ $(ss -Hltnpe 'sport = :19334' | grep -o 'uid:[0-9]*' | head -1) != "uid:$(id -u "$LP_USER")" ]]; then
  give_up "the listener on 19334 isn't $LP_USER's"
fi
systemctl is-active --quiet ampi-checkout-engine.service || give_up "the first engine isn't running"
for u in assistant ampitools; do
  id "$u" >/dev/null 2>&1 || continue
  if sudo -u "$u" /usr/bin/python3 -I -c '
import socket; socket.create_connection(("127.0.0.1", 19334), timeout=3)' 2>/dev/null; then
    give_up "SECURITY: user $u could connect to the engine's port; check the nft table"
  fi
done
echo "    engine port refused for other users: ok"
# A real page, with everything it runs under (firewall, no writable code, filtered system calls).
title=$(ask '{"cmd": "open", "url": "https://example.com/", "engine": "lightpanda"}' snapshot.title) || title=
ask "{\"cmd\": \"close\", \"session\": \"$(ask '{"cmd": "status"}' session || true)\"}" ok >/dev/null || true
[[ $title == "Example Domain" ]] || give_up "it didn't open a page (${title:-no answer}); see: systemctl status ampi-checkout-lp"
echo "    opened a page: ok"
echo "Done."
