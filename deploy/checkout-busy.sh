# Sourced by the installers: is a checkout session open (perhaps paying)? Only a clear "no" counts as no.
checkout_busy() {
  [[ -S /run/ampi-checkout.sock ]] || return 1
  systemctl is-active --quiet ampi-checkout.service || return 1  # not running: no session
  local out
  out=$(sudo -u ampitools /usr/bin/python3 -I -c '
import json, socket
s = socket.socket(socket.AF_UNIX); s.settimeout(20); s.connect("/run/ampi-checkout.sock")
s.sendall(b"{\"cmd\": \"status\"}\n"); r = json.loads(s.recv(4096)); print("free" if r.get("ok") and r.get("session") is None else "busy")' 2>/dev/null) || return 0
  [[ $out != free ]]
}
