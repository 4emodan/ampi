# Ampi companion (Android app): design

Two jobs, two separate levels of trust, one connection:

1. **Phone actions** that Ampi asks for: notifications, alarms, timers; later geofences, sharing to Ampi, opening
   maps or the dialer. Ampi (the model) decides these, so they are limited to what can't hurt much.
2. **The key to saved logins** (later phase): only the sign-in service asks, the owner confirms with a fingerprint.
   Ampi can't ask the phone for this, imitate it or see it.

## Parts

```
phone (Ampi app) ==TLS 1.3, both sides pinned==> ampi-phone gateway (user ampiphone, one public port)
                                                    ^ /run/ampi-phone.sock (group ampiphone-use: only user
                                                    |   ampitools; the caller's uid is checked too)
                                                 ampi-tools: ops phone.* <- `phone` command (Ampi)
owner's PC: companion/build.ps1 = build; scripts/companion.ps1 = install over USB (adb), pair over USB + SSH
root on the server: `ampi-phone offer|code|add|devices|remove|status`
```

### Gateway (`src/ampi_phone/`, Python standard library only, installed root-owned in /usr/local/lib/ampi-phone)

- Socket-activated: systemd (root) owns the public port and the control socket and hands them over. The service has
  no network of its own (`PrivateNetwork`, only AF_UNIX) and may not call connect(), shutdown() or io_uring (the inherited
  listener belongs to the host's network and could otherwise be turned into an outgoing connection). Nobody else can take
  the port while it restarts: the socket stays. The sockets are enabled by the first pairing and disabled when the last
  phone is removed.
- `/etc/ampi-phone/` (root:ampiphone, 0750): server key and certificate (P-256), one file per paired phone. The gateway
  can read its trust list but not change it. `add`/`remove` restart the gateway, which drops every live connection; `remove`
  disconnects before anything else.
- TLS 1.3 only, client certificate required, no session tickets. The trust store holds exactly the paired phones'
  self-signed certificates (partial chain allowed; every phone has its own random subject), and the peer certificate
  must also equal a paired one byte for byte. An outsider reaches OpenSSL's handshake and no code of ours.
- Limits before TLS: 2 handshakes at once per address (/64 for IPv6, all of this machine's own as one) and 48 in all, 5 s
  each, 30 a minute per address. Addresses a paired phone connected from in the last day have wider limits and 16 more
  slots nobody else can take. A flood from many addresses can still delay a phone that comes from a new one
  (or after the gateway restarted: it remembers only while running), and someone behind the same router can use up that
  address's slots. That only delays; it opens nothing.
- Frames: 4-byte big-endian length + UTF-8 JSON object, at most 64 KB, at most 120 a minute from the phone. A malformed
  frame closes the connection; unknown types are ignored.
- The server pings every 170 s; nothing from the phone for 6.5 min, unacknowledged data for 150 s (`TCP_USER_TIMEOUT`), or
  a request without an answer closes the connection. A newer connection from the same phone replaces the older one.
- Own limits, so that a compromised ampi-tools can't flood the phone: known kinds only, 8 KB, 20 a minute, 120 an hour.
- Logs: kinds and outcomes, never titles, texts or labels. Nothing is stored on disk: no queue. An unreachable phone is
  an error ("phone not reachable"), and Ampi then says so in Telegram, which does deliver later.

### Messages

After the handshake the server sends `{"t":"welcome","proto":1,"now":EPOCH}`; the phone counts as connected only then
(TLS 1.3 tells a rejected client so only at its first read). The phone answers `{"t":"hello", ...}`: app version,
Android version, time zone, what is missing (permissions, battery exemption).

Request `{"t":"req","id":ID,"kind":K,"body":{...},"exp":EPOCH}`, answer `{"t":"res","id":ID,"ok":…}`; `ping`/`pong`.
`exp` is server time; the phone measures it with its monotonic clock from the last `now` it got, never with its own
wall clock. A request past `exp` is refused, never done late. Ids are random; a waiting request is sent again on a new
connection, and the phone does a given id only once, but always answers from its stored alarms, never from a cache.

| kind | body | notes |
|---|---|---|
| `notify` | title, text | plain text; the same id never shows twice |
| `alarm.set` | time, date or weekdays, label | answers with the exact moment and the phone's zone; the same kind + time + date/weekdays + label is the same alarm |
| `timer.set` | end (server time), label | |
| `alarm.list` / `alarm.cancel` | - / id | the list includes recent events (rang, stopped, missed, cancelled, undone) |
| `status` | - | |

No answer within 10 s = "unknown: it may or may not have happened; check `phone alarms`".

### ampi-tools: `phone.*`

`phone status | notify TITLE TEXT | alarm TIME [--date D | --days mon,tue] [--label L] | timer 10m [--label L] |
alarms | cancel ID`

- Allow-list in `Daemon.run`: the owner's private chat: yes. Group and forwarded messages: notifications, alarms and
  timers ask the owner, and nothing reaches the phone (or comes back from it) before the owner approves; reads and
  cancelling are refused, and they don't see phone requests in the approvals list. Everything else (watchers old and new, social mode,
  unknown processes) is refused. Watchers report in Telegram; urgent alerts from watchers can come later.
- Cancelling asks the owner too, unless the alarm was set less than an hour ago (a snooze never counts as newly set): a
  tricked Ampi must not be able to silently remove the morning alarm.
- Times become absolute before anyone approves ("timer 10m" = a moment, "07:00" = a date; a group's "07:00" is
  the next one after the request, dated in the phone's zone when the owner approves), and the request expires after 10 minutes.
- One phone change per request; title 80, text 1000, label 60 characters, control and bidi characters removed.
- The phone's warnings are passed on: alarm volume 0, total silence, notifications off, no full-screen permission. The
  phone sends codes for warnings, refusals and events; only words from the server's own list reach Ampi
  (and the alarms' labels, 60 characters each).

### The app (`companion/`, plain Java, no libraries, built with the SDK tools only, no Gradle)

Android 16+ (API 36). Permissions: internet, network state, notifications, exact alarms (`USE_EXACT_ALARM`),
full-screen alarm screen, foreground service (special use; system exempted for ringing), start at boot, vibrate, wake
lock, battery-saver exemption (asked for), fingerprint, hiding overlays. No storage, contacts, location, microphone,
accessibility or notification-reading permission.

- **LinkService**: foreground service (`specialUse`) holding the one connection, on a thread. Reconnects on network
  changes and with back-off (5 s to 15 min, reset only by `welcome`). A wake-up every 15 min
  (`setAndAllowWhileIdle`) closes a connection that has been silent for 4 min (`elapsedRealtime`) and sets every alarm
  again from the file; a due one is handed to Android's alarm clock, which alone may always start the ringing. Each message is handled under a short wake lock. Without the battery exemption this is unreliable: the app says so.
- **TLS**: the server certificate must equal the pinned one (no certificate authorities, no host names); the client key
  (P-256, random subject) is made in the phone's secure hardware and can't be exported. It isn't fingerprint-bound:
  the link must work while the phone is locked.
- **Alarms**: the app's own, `setAlarmClock`, stored in device-protected storage and direct-boot aware, so they ring
  after a reboot before the first unlock. Set again after a reboot, an update, and a time or zone change. More than
  10 min late = a "missed" notification instead of ringing. Rings from a foreground service (`systemExempted`,
  `USAGE_ALARM`) over the lock screen with Stop and Snooze (10 min, stored); stops itself after 10 min ("missed"). A wake lock
  covers the moment between the alarm and the sound. If the file can't be read or is broken, Android's alarm rings anyway.
  The label also shows on the alarm screen while the phone is locked.
  A time that doesn't exist (spring) rings after the jump; one that exists twice (autumn) rings the first time.
- **Every change by Ampi shows a notice** ("Ampi set an alarm: …", "Ampi cancelled …") with Undo (needs unlocking;
  messages can't push these notices out). The phone has its own limits: 50 alarms, 20 set a day, 30 notifications an hour, timers up to 24 h.
- **Notifications**: channels "Ampi", "Alarm changes", "Alarms", "Connection". Hidden on the lock screen by default
  (the owner's lock-screen setting). Tapping opens the app. No links, no system-made actions. At most 20 kept, and the
  answer says so if Android didn't show it.
- Screens: status and what to fix, alarms (delete, test), messages, unpair (needs the screen lock). No WebView,
  `allowBackup=false`, not debuggable; exported: the launcher, and the pairing screen and receiver, which only adb may
  start (`android.permission.DUMP`; an app the owner gave that permission with adb could open the screen too, but can't
  press its buttons).

## Pairing: over the USB cable, nothing on the network

1. `scripts/companion.ps1 -Pair`: `ampi-phone offer` (over SSH) gives the server's address, port and certificate
   (public data); adb starts the pairing screen with it. Only adb can start that screen; it refuses while paired
   (unpair first, with the screen lock).
2. The owner confirms on the phone; it makes its key and shows a code. The script fetches the phone's *public*
   certificate (the result of a broadcast only adb may send) and hands it to `ampi-phone code`, which computes the code
   on the server from both certificates: SHA-256 over a versioned transcript that also has a slot for the sign-in
   service's public key; 26 base32 characters.
3. The owner compares the codes and confirms on both; `ampi-phone add NAME --code …` accepts the phone.

A lost phone: `ampi-phone remove NAME`.

## Later: logins (not in this phase; what this phase already leaves room for)

- Each saved login is encrypted with its own random key; that key is wrapped to (a) a hardware RSA key on the phone
  that needs a fingerprint for every decryption (a `Cipher` in the fingerprint prompt: works on API 36; saving needs
  no prompt) and (b) a recovery key on paper. The server stores only this. A newly added fingerprint erases the phone
  key, so the paper code is the normal way back.
- The phone's login key must be authenticated like its connection key: a new pairing (transcript v2), never over the link
  alone, or a compromised gateway could put in its own.
- `vault` frames are a separate type with their own ids and no re-sending; today's app refuses them. Requests are signed
  by `ampi-signin` (own user and key; the phone learns the public key only while pairing): purpose, phone, site,
  reason, nonce, expiry and the one-time key the answer is encrypted to, so the gateway, ampi-tools and Ampi can
  neither make nor read one. Nonces are remembered until they expire; prompts are rate-limited on the phone; Ampi's
  notifications wait while a prompt is shown and can never open it.

## Order

A. Gateway, `phone` command, app with notifications, alarms, timers, pairing. B. Saving and unlocking logins.
C. Lightpanda engine + sign-in filling + prepaid bookings. D. Card payments on Lightpanda, confirmed in the app.
Each phase: skeptic review of the plan, build, security and correctness reviews, fixes, then the owner deploys.

## Release key

`companion/build.ps1 -Release` signs with a key in `%USERPROFILE%\.ampi-companion` (made on first use, password typed by
the owner). Lose it and the app can only be reinstalled (pair again). Someone with that key *and* the unlocked phone on
a cable could install a changed app: keep a copy offline, and switch USB debugging off when not installing.

## Known limits

- Someone controlling the server can send the phone notifications and alarms, cancel alarms, list them, and read what Ampi
  sends to it (each change still shows its notice).
- Force-stopping the app, or a maker's battery killer, stops the link; `phone status` shows the last contact. Alarms
  already set still ring (unless force-stopped).
- The phone's address is visible to the server; the port answers a TLS handshake to anyone.
