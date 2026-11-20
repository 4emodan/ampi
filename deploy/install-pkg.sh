#!/usr/bin/env bash
# Installs the ampi-pkg root helper: the assistant can ask (via Telegram buttons) for Ubuntu packages,
# and they are installed once you approve. Run as root from a checkout:  sudo bash deploy/install-pkg.sh
# Also needs ask_permissions = true in the profile (config.toml). To remove: systemctl disable --now ampi-pkg
set -euo pipefail
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-pkg.sh" >&2
  exit 1
fi

install -d -o root -g root -m 755 /usr/local/lib/ampi-pkg
install -o root -g root -m 755 "$SRC_DIR/deploy/ampi-pkg.py" /usr/local/lib/ampi-pkg/ampi-pkg.py
install -o root -g root -m 644 "$SRC_DIR/deploy/ampi-pkg.service" /etc/systemd/system/ampi-pkg.service
systemctl daemon-reload
systemctl enable ampi-pkg >/dev/null
systemctl restart ampi-pkg
sleep 1
systemctl is-active ampi-pkg >/dev/null && echo "ampi-pkg is running." || { journalctl -u ampi-pkg -n 20; exit 1; }
cat <<'EOF'
Next: in /home/assistant/config.toml set `ask_permissions = true` in the profile, then
  systemctl restart claude-assistant
Every install request reaches you in Telegram with Approve / Reject; installs are logged:
  journalctl -u ampi-pkg
EOF
