#!/usr/bin/env bash
# Installs or updates the gateway for the owner's phone and the sign-in service (companion app, docs/COMPANION.md).
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
SI_USER=ampisignin
SI_LIB=/usr/local/lib/ampi-signin
SI_CONF=/etc/ampi-signin
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-phone.sh" >&2
  exit 1
fi

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now ampi-signin.socket ampi-signin.service 2>/dev/null || true
  systemctl disable --now ampi-phone.socket ampi-phone.service 2>/dev/null || true
  systemctl stop ampi-phone-vault.socket 2>/dev/null || true
  command -v ufw >/dev/null && ufw --force delete allow "$PORT/tcp" >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ampi-phone.socket /etc/systemd/system/ampi-phone.service /usr/local/sbin/ampi-phone \
    /etc/systemd/system/ampi-phone-vault.socket /etc/systemd/system/ampi-signin.socket /etc/systemd/system/ampi-signin.service
  systemctl daemon-reload
  rm -rf "$LIB_DIR" "$CONF_DIR" "$SI_LIB" "$SI_CONF" /run/ampi-phone-vault /etc/tmpfiles.d/ampi-phone.conf
  userdel "$GW_USER" 2>/dev/null || true
  userdel "$SI_USER" 2>/dev/null || true
  if [[ -f /etc/ampi-checkout/ctl.env ]]; then
    sed -i '/^AMPI_CHECKOUT_SIGNIN_UIDS=/d' /etc/ampi-checkout/ctl.env
    systemctl try-restart ampi-checkout.service || true
  fi
  for g in "$USE_GROUP" ampisignin-use; do
    gpasswd -d "$TOOLS_USER" "$g" >/dev/null 2>&1 || true
    groupdel "$g" 2>/dev/null || true
  done
  groupdel ampiphone-vault 2>/dev/null || true
  echo "Removed. The phone must be paired again after a new install."
  exit 0
fi
if ! id "$TOOLS_USER" >/dev/null 2>&1; then
  echo "User $TOOLS_USER is missing: run deploy/install-tools.sh first" >&2
  exit 1
fi

echo "==> Users $GW_USER and $SI_USER; groups $USE_GROUP and ampisignin-use ($TOOLS_USER only)"
for u in "$GW_USER" "$SI_USER"; do
  getent group "$u" >/dev/null || groupadd --system "$u"
  id "$u" >/dev/null 2>&1 || useradd --system --gid "$u" --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin "$u"
done
# ampiphone-vault has no members: ampi-signin.service gets it as a supplementary group (and the checkout controller's, which
# may not be installed yet).
for g in ampiphone-vault ampicheckoutctl-use; do
  getent group "$g" >/dev/null || groupadd --system "$g"
done
new_member=0
for g in "$USE_GROUP" ampisignin-use; do
  getent group "$g" >/dev/null || groupadd --system "$g"
  id -nG "$TOOLS_USER" | tr ' ' '\n' | grep -qx "$g" || { usermod -aG "$g" "$TOOLS_USER"; new_member=1; }
done

echo "==> Code and config"
install -d -o root -g root -m 755 "$LIB_DIR" "$LIB_DIR/ampi_phone"
for f in __init__.py trust.py gateway.py admin.py; do
  install -o root -g root -m 644 "$SRC_DIR/src/ampi_phone/$f" "$LIB_DIR/ampi_phone/$f"
done
install -o root -g root -m 644 "$SRC_DIR/deploy/ampi-phone-run.py" "$LIB_DIR/run.py"
install -o root -g root -m 755 "$SRC_DIR/deploy/ampi-phone" /usr/local/sbin/ampi-phone
# Only root writes here: the gateway can't change who is paired.
install -d -o root -g "$GW_USER" -m 750 "$CONF_DIR" "$CONF_DIR/devices"
install -d -o root -g root -m 755 "$SI_LIB" "$SI_LIB/ampi_phone"
for f in __init__.py trust.py signin.py; do
  install -o root -g root -m 644 "$SRC_DIR/src/ampi_phone/$f" "$SI_LIB/ampi_phone/$f"
done
install -o root -g root -m 644 "$SRC_DIR/deploy/ampi-signin-run.py" "$SI_LIB/run.py"
install -d -o root -g "$SI_USER" -m 750 "$SI_CONF" "$SI_CONF/devices"
# Each has its own key and certificate. Pairing fixes both: a new one would need the phone to be paired again (and the logins
# saved on it are bound to the sign-in service's).
for c in "$CONF_DIR:$GW_USER:ampi-phone" "$SI_CONF:$SI_USER:ampi-signin"; do
  IFS=: read -r dir group cn <<<"$c"
  if [[ ! -f $dir/server.key || ! -f $dir/server.crt ]]; then
    if compgen -G "$dir/devices/*.json" >/dev/null; then
      echo "$dir: the key or certificate is missing but phones are paired: restore it, or remove them first" >&2; exit 1
    fi
    (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 9000 -sha256 \
      -subj "/CN=$cn" -keyout "$dir/server.key" -out "$dir/server.crt")
  fi
  chown root:"$group" "$dir/server.key" "$dir/server.crt"
  chmod 640 "$dir/server.key" "$dir/server.crt"
done
if [[ ! -f $CONF_DIR/config.json ]]; then
  host=$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')
  [[ $host =~ ^[0-9.]+$ ]] || { echo "can't tell this server's address; write $CONF_DIR/config.json yourself" >&2; exit 1; }
  printf '{"host": "%s", "port": %s}\n' "$host" "$PORT" > "$CONF_DIR/config.json"
fi
chmod 644 "$CONF_DIR/config.json"
printf 'AMPI_PHONE_CLIENT_UIDS=%s\nAMPI_PHONE_VAULT_UIDS=%s\n' "$(id -u "$TOOLS_USER")" "$(id -u "$SI_USER")" > "$CONF_DIR/gateway.env"
printf 'AMPI_SIGNIN_CLIENT_UIDS=%s\n' "$(id -u "$TOOLS_USER")" > "$SI_CONF/signin.env"
# The checkout browser's controller fills logins in for the sign-in service (and does nothing else for it).
ctl=/etc/ampi-checkout/ctl.env
line="AMPI_CHECKOUT_SIGNIN_UIDS=$(id -u "$SI_USER")"
if [[ -f $ctl ]] && ! grep -qx "$line" "$ctl"; then
  sed -i '/^AMPI_CHECKOUT_SIGNIN_UIDS=/d' "$ctl"
  printf '%s\n' "$line" >> "$ctl"
  systemctl try-restart ampi-checkout.service || true
fi

echo "==> systemd units"
# The socket is only restarted when its unit changed: while it listens, nobody else can take the port.
socket_changed=0
cmp -s "$SRC_DIR/deploy/ampi-phone.socket" /etc/systemd/system/ampi-phone.socket || socket_changed=1
vault_changed=0
cmp -s "$SRC_DIR/deploy/ampi-phone-vault.socket" /etc/systemd/system/ampi-phone-vault.socket || vault_changed=1
signin_changed=0
cmp -s "$SRC_DIR/deploy/ampi-signin.socket" /etc/systemd/system/ampi-signin.socket || signin_changed=1
# There at every boot, before the gateway's first start: the sign-in service's view of /run is fixed when it starts.
echo 'd /run/ampi-phone-vault 0755 root root' > /etc/tmpfiles.d/ampi-phone.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ampi-phone.conf
install -m 644 "$SRC_DIR/deploy/ampi-phone.socket" /etc/systemd/system/ampi-phone.socket
for f in ampi-phone.service ampi-phone-vault.socket ampi-signin.socket ampi-signin.service; do
  install -m 644 "$SRC_DIR/deploy/$f" "/etc/systemd/system/$f"
done
systemctl daemon-reload
if compgen -G "$CONF_DIR/devices/*.json" >/dev/null; then
  systemctl enable ampi-phone.socket >/dev/null 2>&1
  if [[ $socket_changed == 1 ]]; then
    systemctl stop ampi-phone.service ampi-phone.socket 2>/dev/null || true
  elif [[ $vault_changed == 1 ]]; then
    systemctl stop ampi-phone-vault.socket 2>/dev/null || true  # (the service's restart below starts it as it is now)
  fi
  systemctl start ampi-phone.socket
  systemctl restart ampi-phone.service
  sleep 1
  systemctl is-active --quiet ampi-phone.service || { echo "the gateway didn't start: journalctl -u ampi-phone -n 30" >&2; exit 1; }
  systemctl is-active --quiet ampi-phone-vault.socket || { echo "ampi-phone-vault.socket isn't listening" >&2; exit 1; }
  echo "    running: $(ls "$CONF_DIR/devices" | wc -l) paired phone(s)"
else
  echo "    no phone paired yet: nothing listens. On your PC: scripts/companion.ps1 -Install -Pair"
fi
# It listens only on a Unix socket; without a paired phone it says so.
systemctl stop ampi-signin.service 2>/dev/null || true
if [[ $signin_changed == 1 ]]; then systemctl stop ampi-signin.socket 2>/dev/null || true; fi
systemctl enable --now ampi-signin.socket >/dev/null 2>&1
systemctl is-active --quiet ampi-signin.socket || { echo "ampi-signin.socket isn't listening" >&2; exit 1; }
# Group membership only applies to new processes.
if [[ $new_member == 1 ]] && systemctl is-active --quiet ampi-tools; then systemctl restart ampi-tools; fi
echo "Done."