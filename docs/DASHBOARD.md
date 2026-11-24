# The dashboard on the web

The hub's pages (overview, abilities, watchers, activity; not the address book) at `https://HOST/ampi/`.

- **Signing in:** send `/dashboard` to Ampi in the private chat. The link works once, for 5 minutes. A session lasts 12 hours at most
  and ends after an hour without use. `/dashboard off` signs out everywhere. Every sign-in is reported in Telegram.
- **Changing:** stricter settings apply at once and are reported; looser ones wait for your approval in Telegram.
- **Parts:** nginx (HTTPS) → `ampi-dash` (user `ampidash`, no data, no network) → `ampi-tools`, which checks the session on every
  request and lets that user ask for nothing else.
- **HTTPS:** a Let's Encrypt certificate for the IP address (about 6 days, renewed by `ampi-cert.timer`; Telegram tells you if not).
- **Install:** `CERTBOT_VERSION=5.4.0 bash deploy/install-dash.sh HOST`, remove with `--uninstall`.
