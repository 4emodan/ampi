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

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now ampi-dash.socket ampi-dash.service ampi-cert.timer 2>/dev/null || true
  rm -f /etc/nginx/sites-enabled/ampi /etc/nginx/sites-available/ampi /etc/nginx/conf.d/ampi-limits.conf
  sed -i '\#include /etc/nginx/snippets/ampi-acme.conf;#d' "$SITE80" 2>/dev/null || true
  rm -f /etc/nginx/snippets/ampi-acme.conf
  nginx -t && systemctl reload nginx
  ufw --force delete allow 443/tcp >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/ampi-dash.s* /etc/systemd/system/ampi-cert.*
  systemctl daemon-reload
  [[ ! -f $CONF ]] || sed -i '/^\[web\]/,/^url = /d' "$CONF"
  rm -rf "$LIB" /etc/ampi-dash /opt/certbot-ampi
  userdel "$DASH" 2>/dev/null || true
  systemctl restart ampi-tools
  echo "Removed (the certificate's folder /etc/letsencrypt-ampi is kept)."
  exit 0
fi

HOST=${1:-}
[[ $HOST =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "Give this server's public IPv4 address" >&2; exit 1; }
ip -4 -o addr show | grep -qF " $HOST/" || { echo "$HOST isn't an address of this server" >&2; exit 1; }
[[ $CERTBOT_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Set CERTBOT_VERSION (5.4.0 or newer)" >&2; exit 1; }
[[ -f $SITE80 ]] || { echo "$SITE80 is missing" >&2; exit 1; }

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
cp -an "$SITE80" "/root/nginx-site80-before-ampi.conf"
grep -qF 'include /etc/nginx/snippets/ampi-acme.conf;' "$SITE80" || \
  sed -i '0,/^[[:space:]]*server_name .*;/s##&\n    include /etc/nginx/snippets/ampi-acme.conf;#' "$SITE80"
[[ $(grep -cF 'include /etc/nginx/snippets/ampi-acme.conf;' "$SITE80") == 1 ]] || { echo "couldn't change $SITE80" >&2; exit 1; }
nginx -t 2>/dev/null || { cp -a /root/nginx-site80-before-ampi.conf "$SITE80"; nginx -t; echo "nginx refused it; put back" >&2; exit 1; }
systemctl reload nginx

echo "==> Certbot $CERTBOT_VERSION"
if [[ $(/opt/certbot-ampi/bin/certbot --version 2>/dev/null) != "certbot $CERTBOT_VERSION" ]]; then
  python3 -m venv --clear /opt/certbot-ampi
  /opt/certbot-ampi/bin/pip install -q --disable-pip-version-check --only-binary :all: "certbot==$CERTBOT_VERSION"
fi
get() {
  /opt/certbot-ampi/bin/certbot certonly "${LE[@]}" --non-interactive --agree-tos --register-unsafely-without-email \
    --preferred-profile shortlived --webroot -w /var/www/acme --cert-name ampi --keep-until-expiring --ip-address "$HOST" "$@"
}
if [[ ! -f /etc/letsencrypt-ampi/live/ampi/fullchain.pem ]]; then
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
ufw allow 443/tcp comment ampi-dash >/dev/null

echo "==> ampi-tools"
if ! grep -q '^\[web\]' "$CONF"; then
  printf '\n[web]\nurl = "https://%s"\n' "$HOST" >> "$CONF"
fi
systemctl restart ampi-tools
sleep 3

echo "==> Check"
curl -sS -o /dev/null -w '    page: %{http_code}\n' "https://$HOST/ampi/"
curl -sS -o /dev/null -w '    without signing in: %{http_code}\n' -H 'X-Ampi-Hub: web' "https://$HOST/ampi/api/overview"
curl -sS -o /dev/null -w '    anything else: %{http_code}\n' "https://$HOST/"
echo "Done. Send /dashboard to Ampi."
