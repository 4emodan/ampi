#!/usr/bin/env bash
# The dashboard on the web (https://HOST/ampi/), docs/DASHBOARD.md. Run as root from a checkout, after install-tools.sh:
#   sudo bash deploy/install-dash.sh HOST          HOST: this server's public IPv4 address
#   sudo bash deploy/install-dash.sh --uninstall
# It opens port 443, installs Certbot in /opt/certbot-ampi (apart from the system's) and gets a Let's Encrypt certificate.
# By running it you accept Let's Encrypt's Subscriber Agreement. Nothing on port 80 changes, except the path it checks.
set -euo pipefail

CERTBOT_VERSION=${CERTBOT_VERSION:-}
DASH=ampidash
LIB=/usr/local/lib/ampi-dash
LE=(--config-dir /etc/letsencrypt-ampi --work-dir /var/lib/letsencrypt-ampi --logs-dir /var/log/letsencrypt-ampi)
SRC=$(cd "$(dirname "$0")/.." && pwd)
SITE80=${SITE80:-/etc/nginx/sites-available/dashboard}
CONF=/home/ampitools/config.toml
[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

SITE80=$(readlink -f "$SITE80" 2>/dev/null || echo "$SITE80")

# Writes (or, with "", removes) our own marked lines in config.toml; nothing else there is touched.
web_config() {
  [[ -f $CONF ]] || return 0
  python3 - "$CONF" "$1" <<'PY'
import os, sys, tomllib
path, url = sys.argv[1:]
text = open(path, encoding="utf-8").read()
a, b = "# >>> ampi-dash", "# <<< ampi-dash"
if text.count(a) == text.count(b) == 1 and text.index(a) < text.index(b):
    text = text[:text.index(a)].rstrip("\n") + "\n" + text[text.index(b) + len(b):].lstrip("\n")
elif a in text or b in text:
    sys.exit(f"{path}: the ampi-dash lines were changed by hand; edit [web] yourself")
if url:
    if "web" in tomllib.loads(text):
        sys.exit(f"{path} already has a [web] section of its own")
    text = text.rstrip("\n") + f'\n\n{a}\n[web]\nurl = "{url}"\n{b}\n'
tomllib.loads(text)
st = os.stat(path)
tmp = path + ".new"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(text)
os.chmod(tmp, st.st_mode & 0o777)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, path)
PY
}

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now ampi-dash.socket ampi-dash.service ampi-cert.timer 2>/dev/null || true
  rm -f /etc/nginx/sites-enabled/ampi /etc/nginx/sites-available/ampi /etc/nginx/conf.d/ampi-limits.conf
  sed -i '\#include /etc/nginx/snippets/ampi-acme.conf;#d' "$SITE80" 2>/dev/null || true
  rm -f /etc/nginx/snippets/ampi-acme.conf
  nginx -t && systemctl reload nginx
  [[ ! -f /etc/ampi-dash/ufw ]] || ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ampi-dash.s* /etc/systemd/system/ampi-cert.*
  systemctl daemon-reload
  web_config ""
  rm -rf "$LIB" /etc/ampi-dash /opt/certbot-ampi
  userdel "$DASH" 2>/dev/null || true
  systemctl restart ampi-tools
  echo "Removed (the certificate's folder /etc/letsencrypt-ampi is kept)."
  exit 0
fi

HOST=${1:-}
[[ $HOST =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "Give this server's public IPv4 address" >&2; exit 1; }
ip -4 -o addr show | grep -qF " $HOST/" || { echo "$HOST isn't an address of this server" >&2; exit 1; }
[[ $CERTBOT_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Set CERTBOT_VERSION (5.10.0 or newer)" >&2; exit 1; }
[[ -f $SITE80 ]] || { echo "$SITE80 is missing" >&2; exit 1; }

grep -q '^DASH_USER' /home/ampitools/app/src/ampi_tools/server.py || { echo "Install the current ampi-tools first" >&2; exit 1; }
nginx -t 2>/dev/null || { nginx -t || true; echo "nginx's present configuration has an error: not touching it" >&2; exit 1; }

echo "==> The service"
getent group "$DASH" >/dev/null || groupadd --system "$DASH"
id "$DASH" >/dev/null 2>&1 || useradd --system --gid "$DASH" --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$DASH"
install -d -o root -g root -m 755 "$LIB" "$LIB/static" /etc/ampi-dash
install -o root -g root -m 644 "$SRC/src/ampi_dash/dash.py" "$LIB/dash.py"
for f in index.html abilities.html watchers.html activity.html hub.css hub.js overview.js abilities.js watchers.js activity.js; do
  install -o root -g root -m 644 "$SRC/src/ampi_tools/hub_static/$f" "$LIB/static/$f"
done
printf 'AMPI_DASH_URL=https://%s\n' "$HOST" > /etc/ampi-dash/env
for f in ampi-dash.socket ampi-dash.service ampi-cert.service ampi-cert.timer; do
  install -m 644 "$SRC/deploy/$f" "/etc/systemd/system/$f"
done
systemctl daemon-reload
systemctl enable --now ampi-dash.socket >/dev/null 2>&1
systemctl try-restart ampi-dash.service

echo "==> Port 80: only the path Let's Encrypt checks"
install -d -o root -g root -m 755 /var/www/acme /var/www/acme/.well-known /var/www/acme/.well-known/acme-challenge
printf 'location ^~ /.well-known/acme-challenge/ { root /var/www/acme; default_type text/plain; try_files $uri =404; }\n' \
  > /etc/nginx/snippets/ampi-acme.conf
probe=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
printf '%s' "$probe" > "/var/www/acme/.well-known/acme-challenge/$probe"
chmod 644 "/var/www/acme/.well-known/acme-challenge/$probe"
reached() { [[ $(curl -s --max-time 10 "http://$HOST/.well-known/acme-challenge/$probe") == "$probe" ]]; }
if ! reached; then
  before=$(mktemp /root/nginx-site80-before-ampi.XXXXXX)
  cp -a "$SITE80" "$before"
  put_back() { cat "$before" > "$SITE80"; nginx -t -q && systemctl reload nginx; rm -f "/var/www/acme/.well-known/acme-challenge/$probe"; }
  grep -qF 'include /etc/nginx/snippets/ampi-acme.conf;' "$SITE80" || \
    sed -i '0,/^[[:space:]]*server_name .*;/s##&\n    include /etc/nginx/snippets/ampi-acme.conf;#' "$SITE80"
  nginx -t 2>/dev/null || { put_back; echo "nginx refused the change to $SITE80; put back" >&2; exit 1; }
  systemctl reload nginx
  sleep 1
  reached || { put_back; echo "$SITE80 isn't the site on port 80 (or it sends everything elsewhere); put back" >&2; exit 1; }
  echo "    $SITE80 changed (one line); before: $before"
fi
rm -f "/var/www/acme/.well-known/acme-challenge/$probe"

echo "==> Certbot $CERTBOT_VERSION"
if [[ $(/opt/certbot-ampi/bin/certbot --version 2>/dev/null) != "certbot $CERTBOT_VERSION" ]]; then
  python3 -m venv --clear /opt/certbot-ampi
  /opt/certbot-ampi/bin/pip install -q --disable-pip-version-check --only-binary :all: "certbot==$CERTBOT_VERSION"
fi
get() {
  /opt/certbot-ampi/bin/certbot certonly "${LE[@]}" --non-interactive --agree-tos --register-unsafely-without-email \
    --preferred-profile shortlived --webroot -w /var/www/acme --cert-name ampi --keep-until-expiring --renew-with-new-domains --ip-address "$HOST" "$@"
}
cert=/etc/letsencrypt-ampi/live/ampi/fullchain.pem
if [[ ! -f $cert ]] || ! openssl x509 -in "$cert" -noout -checkip "$HOST" | grep -q 'does match' \
    || ! openssl x509 -in "$cert" -noout -checkend 43200 >/dev/null; then
  echo "    a trial first (Let's Encrypt's test server)"
  get --dry-run -q
  get -q
fi
openssl x509 -in /etc/letsencrypt-ampi/live/ampi/fullchain.pem -noout -issuer -enddate -ext subjectAltName

echo "==> HTTPS: only /ampi/"
install -m 644 "$SRC/deploy/nginx-ampi-limits.conf" /etc/nginx/conf.d/ampi-limits.conf
sed "s/@HOST@/$HOST/g" "$SRC/deploy/nginx-ampi.conf" > /etc/nginx/sites-available/ampi
ln -sfn ../sites-available/ampi /etc/nginx/sites-enabled/ampi
nginx -t 2>/dev/null || { rm -f /etc/nginx/sites-enabled/ampi; nginx -t; echo "nginx refused the new site; left out" >&2; exit 1; }
systemctl reload nginx
systemctl enable --now ampi-cert.timer >/dev/null 2>&1
if ! ufw status | grep -q '^443/tcp'; then
  ufw allow 443/tcp comment ampi-dash >/dev/null
  touch /etc/ampi-dash/ufw  # ours to remove
fi

echo "==> ampi-tools"
web_config "https://$HOST"
systemctl restart ampi-tools
sleep 3

echo "==> Check"
curl -sS -o /dev/null -w '    page: %{http_code}\n' "https://$HOST/ampi/"
curl -sS -o /dev/null -w '    without signing in: %{http_code}\n' -H 'X-Ampi-Hub: web' "https://$HOST/ampi/api/overview"
curl -sS -o /dev/null -w '    anything else: %{http_code}\n' "https://$HOST/"
echo "Done. Send /dashboard to Ampi."
