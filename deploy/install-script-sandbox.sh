#!/usr/bin/env bash
# Installs or updates the sandbox that runs watcher scripts (watchers v2) for Claude Assistant.
# Run as root from a checkout, after deploy/install.sh:  sudo bash deploy/install-script-sandbox.sh
#
# Watcher scripts are written by an AI and run unattended, so each run gets a fresh socket-activated
# ampi-script@ instance as user ampiscript: no network, no sockets, no home directories, a read-only system,
# a private /tmp (deploy/ampi-script@.service). Only the assistant user (the bridge) may connect.
set -euo pipefail

LIB_DIR=/usr/local/lib/ampi-script
CONF_DIR=/etc/ampi-script
SCRIPT_USER=ampiscript
SOCKET_GROUP=ampiscript-use
APP_USER=assistant
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install-script-sandbox.sh" >&2
  exit 1
fi
if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "user $APP_USER is missing: run deploy/install.sh first" >&2
  exit 1
fi

echo "==> Users: $SCRIPT_USER (runs scripts), group $SOCKET_GROUP (may use the socket)"
getent group "$SCRIPT_USER" >/dev/null || groupadd --system "$SCRIPT_USER"
id "$SCRIPT_USER" >/dev/null 2>&1 || useradd --system --gid "$SCRIPT_USER" --no-create-home \
  --home-dir /nonexistent --shell /usr/sbin/nologin "$SCRIPT_USER"
getent group "$SOCKET_GROUP" >/dev/null || groupadd --system "$SOCKET_GROUP"
usermod -aG "$SOCKET_GROUP" "$APP_USER"
if id -nG "$SCRIPT_USER" | tr ' ' '\n' | grep -qx "$SOCKET_GROUP"; then
  gpasswd -d "$SCRIPT_USER" "$SOCKET_GROUP" >/dev/null
fi

echo "==> Runner and minimal passwd/group for the sandbox"
install -d -o root -g root -m 755 "$LIB_DIR" "$CONF_DIR"
install -o root -g root -m 644 "$SRC_DIR/src/ampi_tools/script_runner.py" "$LIB_DIR/script_runner.py"
uid=$(id -u "$SCRIPT_USER"); gid=$(id -g "$SCRIPT_USER")
printf '%s\n' "root:x:0:0::/nonexistent:/usr/sbin/nologin" \
  "$SCRIPT_USER:x:$uid:$gid::/nonexistent:/usr/sbin/nologin" > "$CONF_DIR/passwd"
printf '%s\n' "root:x:0:" "$SCRIPT_USER:x:$gid:" > "$CONF_DIR/group"
chmod 644 "$CONF_DIR/passwd" "$CONF_DIR/group"

echo "==> systemd units"
for unit in ampi-script.socket ampi-script@.service; do
  install -m 644 "$SRC_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable ampi-script.socket >/dev/null 2>&1
systemctl restart ampi-script.socket

echo "==> Check through the real sandbox"
probe=$(cat <<'EOF'
import json, socket
code = (
    "import json, socket, sys, os\n"
    "d = json.load(sys.stdin)\n"
    "try:\n"
    "    socket.socket(socket.AF_INET, socket.SOCK_STREAM); net = 'open'\n"
    "except OSError:\n"
    "    net = 'blocked'\n"
    "home = os.path.exists('/home/assistant')\n"
    "open('space/out.txt', 'w').write(d['x'] * 2)\n"
    "print(json.dumps({'net': net, 'home': home, 'x': d['x']}))\n"
)
s = socket.socket(socket.AF_UNIX)
s.settimeout(60)
s.connect("/run/ampi-script.sock")
s.sendall(json.dumps({"code": code, "input": {"x": "ok"}, "space": {}, "timeout": 20}).encode() + b"\n")
buf = b""
while not buf.endswith(b"\n"):
    chunk = s.recv(65536)
    if not chunk:
        break
    buf += chunk
r = json.loads(buf)
assert r["ok"], r
assert r["output"] == {"net": "blocked", "home": False, "x": "ok"}, r["output"]
assert "out.txt" in r["space_changes"], r
print("    sandbox works: no network, no home directories, space files returned")
EOF
)
if ! sudo -u "$APP_USER" python3 -I -c "$probe"; then
  echo "The sandbox check failed; see: journalctl -u 'ampi-script@*' -n 50" >&2
  exit 1
fi
# Group membership only applies to new processes.
systemctl is-active --quiet claude-assistant && systemctl restart claude-assistant
echo "Done."
