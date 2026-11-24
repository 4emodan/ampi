# Ampi companion (Android app): design

Two jobs, two separate levels of trust, one connection:

1. **Phone actions** that Ampi asks for: notifications, alarms, timers; later geofences, sharing to Ampi, opening
   maps or the dialer. Ampi (the model) decides these, so they are limited to what can't hurt much.
2. **The owner's saved logins**: only the sign-in service asks, the owner confirms with a fingerprint. Ampi can't imitate
   that or see a login.

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

1. `scripts/companion.ps1 -Pair`: the installed app must be byte for byte the one built on this PC; `ampi-phone offer` (over SSH) gives the server's address, port and certificate
   (public data); adb starts the pairing screen with it. Only adb can start that screen; it refuses while paired
   (unpair first, with the screen lock).
2. The owner confirms on the phone; it makes its key and shows a code. The script fetches the phone's *public*
   certificate (the result of a broadcast only adb may send) and hands it to `ampi-phone code`, which computes the code
   on the server from three certificates (gateway, phone, sign-in service): SHA-256 over a versioned transcript; 26 base32
   characters. `add` accepts the phone for both services.
3. The owner compares the codes and confirms on both; `ampi-phone add NAME --code …` accepts the phone.

A lost phone: `ampi-phone remove NAME`.

## Saved logins and signing in (phases B and C1)

The owner's logins exist **only on the phone**: the server has neither a key to them nor an encrypted copy. Unlocking one needs
the owner's fingerprint there, every time. `signin test example.com` only tests (nothing of the login is sent); `signin fill`
signs in on the page open in the checkout browser.

```
Ampi -- `signin` --> ampi-tools (signin.test, private chat only) -- /run/ampi-signin.sock -->
  ampi-signin (user ampisignin, no network) -- /run/ampi-phone-vault/vault.sock -->
    ampi-phone gateway: passes bytes  ==a second TLS connection the phone opens==  phone
                  \_______________ TLS 1.3 inside, ampi-signin <-> phone, both pinned _______________/
```

- **On the phone** (`Logins`): one AES-256-GCM key in the secure hardware, a class-3 biometric for every single use, saving
  included: any fingerprint enrolled on the phone (Android can't tell fingers apart, and on phones that have it also strong face
  unlock). A newly added one, none left or no screen lock destroys the key (then the logins are entered again: they belong in
  a password manager too; the app says to look for fingerprints that aren't the owner's first). Bound into every login: its id, sites, label, and the sign-in service it was saved for,
  so re-pairing the phone with another server makes them useless there. Nothing can be shown again. Sites are exact https
  host names (ASCII; `www.` is the same site), up to 5 per login. Refused, on the phone and by ampi-signin: mail and sign-in
  providers, servers and code, password managers, Telegram and Anthropic, banks and payment services (`NOT`, the same list on both
  sides): one of those opens much more than one site, is how Ampi itself is controlled, or is about money.
- **Asking** (`Unlock`, `UnlockActivity`): a notification without the site -> tap -> the screen with the site, which login, and
  "Use fingerprint" / "Refuse". One at a time, 75 s at most (monotonic; 120 s where a code has to be looked up), 6 in 10 minutes, 30 a day for all sites together, saved
  or not; after a refusal none for a minute. What doesn't depend on the site is checked first, and asking for one that isn't saved
  shows a notice: nobody finds out unseen which sites there are. All words are the app's own. While it is open, Ampi's messages, alarms and cancellations are refused
  (`signin_open`); if something rings, the request ends ("interrupted"); while something rings, none starts. When the server or
  the channel goes away, so does the request. Only Refuse is a refusal: a closed or timed-out fingerprint prompt goes back to the
  screen.
- **The channel** (`VaultSession`, `Tls`): the gateway only tells the phone that a channel is wanted (`vault.open` with a
  random number). The phone opens a second connection to the same port, says that number, and inside it starts TLS 1.3 with
  ampi-signin: the phone's certificate is its connection key, the service's is the one fixed while pairing. Nothing is
  looked at before both are proved, so the gateway can neither ask, nor read, nor change anything: it can only not pass it on.
  One request per channel, which must come within 10 s of the handshake; the phone ends a channel after 110 s (155 s for a code)
  whatever arrives.
  One channel at a time, 6 a minute, 40 an hour (both sides count for themselves), 64 KB each way. Unpairing ends it.
- **ampi-signin** (`src/ampi_phone/signin.py`, standard library): socket-activated, only ampi-tools may call it (uid checked), one
  request at a time (others are told "busy" at once, never queued), ends when the command that asked is gone or after 100 s,
  exits when idle. Its own
  key proves who is asking; it opens nothing. Sandbox as the gateway's, plus: of /run it sees only the vault socket and the checkout controller's.
  Journal: outcomes, never sites.
- **ampi-tools** (`ops_signin.py`): the private chat only. Outcomes become words from its own list (and the controller's own
  sentences about a form).

Inside the channel (4-byte length + JSON, 8 KB): `{"v":1,"op":"get","purpose":"test"|"fill","host","wait"}` -> `{"asking":true}`
then `{"ok":true}` (fill: with `user`, `secret` and the login's `hosts`) or `{"ok":false,"error":CODE}`; with `"code":true` also `code`.
`{"v":1,"op":"code","host","wait"}` -> `{"ok":true,"code","hosts"}`. Anything else is answered `unsupported`.

### Signing in on a page

Ampi finds the fields and says which is which (`signin fill --user f0:3 --password f0:4 --press f0:5`); everything else happens
without it:

1. ampi-signin asks the checkout controller (`bind_login`; the controller lets this user ask for nothing else): which site is it?
   The answer comes from the page itself: https, the main frame's real host, and the form must be one that signs in there: a real
   password field (exactly one, not `new-password`) with a user name field that looks like one, in the same form, visible, and
   no other fields to fill in (that would be a registration, or a change of the account). A lone password field ("is it really
   you?") counts only as the second page of a sign-in. Never GET (a form that names no method is made POST). Nothing typed into
   that page by Ampi, no captcha, and one sign-in per session.
   The button is pressed only if it is the submit button of that form, says that it signs in and nothing else (a list of whole
   captions), and there is no price, total or payment frame on the page. Any other button is for `checkout submit`, which asks.
2. ampi-signin asks the phone for exactly that host. The caller can't name one. Then it compares what the login is for with where
   the form sends it, and with every site this browser session was on (a page of some other site could still have a hand in it).
3. The login goes ampi-signin -> controller (`fill_login`) only. The controller checks everything again (same document, same
   elements, nothing done in between) and fills in one go; the page's own scripts get no chance in between.
4. From then on user name and password are cut out of everything of the page that the controller answers (as they are, URL- or
   HTML-written; a field the password was put into stays secret even when the page shows it as text). In the owner's account
   almost every button, switch and link that could do something asks them (`checkout submit`); only a list of captions that look
   around doesn't, and other addresses can't be opened. With sign-ins installed, an engine is never used for a second session.

ampi-signin keeps a login only until it was filled in, or 3 minutes from the fingerprint for the second page of the same browser
session, on a host of the same login; then its process ends (Python can't wipe a string, the end of a process does). What the engine and the site's
scripts see can't be limited: it is the site's own page. The site is as honest as the engine that reports it.

Who could do what: the gateway (reached from the internet): delay. ampi-tools, or a tricked Ampi: make the phone ask (limited,
it shows the site), learn whether a login was unlocked, and, when the owner approves, use the signed-in page: a tricked Ampi within
the rules above; ampi-tools itself applies the rules, so a compromised one could do in that account what the session can. ampi-signin or root: get the one login the owner approves, at that moment. Someone with the unlocked phone but not the finger: see sites and labels, delete.

### One-time codes (two-factor)

`signin code --code f0:7 [--press f0:8]`, right after `signin fill`. The owner types the code on the phone (message, email, another
app, backup code), or the phone works it out from an authenticator key saved with the login (`Totp`: optional, in the same encrypted
record, with a fingerprint of its own; the key stays on the phone). Ampi and ampi-tools never see it.

Such codes also confirm payments, new phone numbers and the like, and Ampi may be tricked by a page. So a code goes in only where the
sign-in itself led, without anyone judging what a page says:

- **The controller's chain** (`Session.chain`): `gen` counts everything that is done in a session. Filling a login in sets `chain = gen`;
  the owner-approved press of that form's own button, or of a button or link whose whole caption is a known step ("Send code", "Try another
  way", …), carries it on (4 at most, nothing to pay on the page), and so do a click on a cookie banner and on a radio button that says which
  way the code should come. Anything else that was done ends it for that session. Codes: 10 minutes, three tries, all in the same field.
- Never after Ampi opened an address with another one in it (`?next=…`), or of a settings, security or payment page; nor where the
  sign-in page's own address leads to one: signing in there could lead straight to such a confirmation.
- Never on a page with a total, a payment frame or a captcha, nor where title, headings or the form speak of paying, transferring, deleting,
  a new number or email, a password (`CODE_DANGER`): more of a net than a wall.
- The field must look like one for a code (or 4 to 10 boxes of one character), in the main frame, with nothing else to fill in; the
  form goes where the login's sites are, never with GET.
- **The phone** gives a code only for the login it sent within the last 10 minutes, only for a site of that login, three times at most, not
  after a refusal; the screen says to read the message the code came in. It returns the login's sites, and ampi-signin compares them
  again with where this session was and where the form sends it.
- **Ampi never types into a code's field** (`code_field`, also in `checkout type`, `fill` and `submit --option`): it reads the owner's
  email, where some codes arrive.
- "Approve it on your other device": Ampi says to open the app, never which number to pick there: a hostile page could make the owner
  approve somebody else's sign-in.

ampi-tools applies the approvals, so a compromised one could press those known steps without asking; it can't get a code anywhere else.

### Not yet

Sign-in forms and codes inside frames, several boxes for a code next to a password, links by email as a sign-in (Ampi reads that mail:
never), Google and the like, passkeys. Never a CAPTCHA.

## Order

A. Gateway, `phone` command, app with notifications, alarms, timers, pairing (live). B. Saving and unlocking logins.
C1. Signing in on a page, one-time codes. C2. Lightpanda engine, prepaid bookings. D. Card payments on Lightpanda, confirmed in the app.
Each phase: skeptic review of the plan, build, security and correctness reviews, fixes, then the owner deploys.

## Release key

`companion/build.ps1 -Release` signs with a key in `%USERPROFILE%\.ampi-companion` (made on first use, password typed by
the owner). Lose it and the app can only be reinstalled (pair again; saved logins are gone). Someone with that key *and* the unlocked phone on
a cable could install a changed app: keep a copy offline, and switch USB debugging off when not installing.

## Known limits

- Someone controlling the server can send the phone notifications and alarms, cancel alarms, list them, and read what Ampi
  sends to it (each change still shows its notice).
- Force-stopping the app, or a maker's battery killer, stops the link; `phone status` shows the last contact. Alarms
  already set still ring (unless force-stopped).
- The phone's address is visible to the server; the port answers a TLS handshake to anyone.
