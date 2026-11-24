# Scenarios: bookings by a fixed script

`checkout flows` lists them, `checkout flow NAME DATE TIME` runs one (`src/ampi_tools/ops_flow.py`). Ampi only chooses which one and when;
the script carries it out after one approval and one fingerprint on the phone.

- **The contract** is a JSON file in `/home/ampitools/flows/` (root-owned, readable by ampitools; not in this repository: it says where the
  owner goes). It names the site, the captions that are pressed, where the slots are, what has to stand next to the button that books, which fields
  may be filled in, and what the confirmation looks like. Its hash is part of the approval: a changed contract needs a new one.
- **Every press** goes through the checks Ampi's own presses go through (`commits`, `_resolve_submit`, `submit`); the contract stands in for the
  owner's look at each, and can't allow what those refuse. Any total, payment frame, captcha, other site, price, word about money next to the button, or unknown filled-in field or value stops it.
- **One press that books.** The journal (`flows.sqlite3`) says "pressing" before it. If what happened isn't clear, the slot counts as taken, and only
  `--again`, which the approval shows, goes past that. `max_per_day` limits each site.
- While it runs, nothing else uses the checkout browser. It closes its session at the end.
- `fields`: what may be filled in, each with a pattern, `@login` (what the sign-in gave) or `@me.…` (the owner's stored detail). `amounts`: prices that
  may stand elsewhere on the page (a banner). `zone`: what the calendar says about its time zone, `{offset}` as GMT+2.
- Trust: the site's own page reports its state; a dishonest site could book something else. ampi-tools applies the rules.

The site must be one the owner's login is saved for. Times are the contract's time zone. Never for watchers, groups or forwarded messages.

```json
{"title": "Evening class", "place": "Example Studio, Main Street 1", "site": "https://www.club.example",
 "start": "https://www.club.example/class", "engine": "lightpanda", "timezone": "Europe/Helsinki", "frames": [],
 "login": {"user": "Email", "button": ["Log In"], "done": "Hello"},
 "steps": [{"press": ["Decline All"], "optional": true}, {"press": ["Log In"]}, {"login": true}, {"press": ["Book Now"]},
           {"slot": {"name": "slots-{date}T00:00:00", "time": "12h", "next": ["Show next week"], "zone": "Time Zone ({offset})"}}, {"press": ["Next"]}],
 "check": ["Evening class", "{when}", "Number of participants 1"], "fields": {"Name": "@me.name", "Email": "@login", "Note": "x?"}, "amounts": ["€80"],
 "book": ["Book Now"], "confirm": {"text": ["Evening class", "Total:\\s*0[.,]00", "{day} .{3,9} {year},? {hour12}:{minute}"], "order": "Order number:\\s*(\\d+)"}, "max_per_day": 2}
```
