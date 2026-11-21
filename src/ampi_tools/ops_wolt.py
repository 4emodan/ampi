"""Wolt operations (registered into ops.OPS when ops.py loads).

Looking things up is safe. Filling a basket in the owner's account is safe too when there's no basket at that
venue yet (it costs nothing: the owner checks out and pays in the Wolt app); replacing an existing basket asks
first, and so does any basket asked for from a group, watcher or forwarded-message run (server.py).
Nothing here can place or pay for an order.
"""

from __future__ import annotations

import json
from typing import Any

from .maps import latlng
from .ops import FIELDS, OPS, PRE_RESOLVE, Op, Result, Services, _bool, need
from .todoist import ToolError
from .wolt import Wolt, build_basket, item_id, money, one_line, share_url, slug, text

MAX_LINES = 20
BASKET_OPS = {"wolt.basket", "wolt.basket_replace"}


def wo(s: Services) -> Wolt:
    if s.wolt_client is None:
        raise ToolError("Wolt isn't set up")
    return s.wolt_client


def _near(v: Any) -> dict:
    ll = latlng(v) if isinstance(v, str) else None
    if not ll:
        raise ToolError("near must be lat,lng (e.g. 60.17,24.94)")
    return ll


def _basket_items(v: Any) -> list[dict]:
    if not isinstance(v, list) or not 1 <= len(v) <= MAX_LINES:
        raise ToolError(f"items must be a list of 1-{MAX_LINES} lines")
    out = []
    for line in v:
        if not isinstance(line, dict) or set(line) - {"id", "count", "options"}:
            raise ToolError('each line is {"id": ITEM_ID, "count": N, "options": {OPTION: CHOICE}}')
        item_id(line.get("id"))
        opts = line.get("options") or {}
        if not isinstance(opts, dict) or len(json.dumps(opts)) > 2000:
            raise ToolError("options must be a small object {option: choice or [choices]}")
        for k, val in opts.items():
            text(k, 100)
            for choice in ([val] if isinstance(val, str) else val if isinstance(val, list) else [None]):
                text(choice, 100)
        out.append({"id": line["id"], "count": line.get("count", 1), "options": opts})
    return out


FIELDS.update({
    "venue": slug, "item": item_id, "items": _basket_items, "lat_lng": _near,
    "search": lambda v: text(v, 100), "open": _bool, "order_id": lambda v: text(v, 40),
})


# --- formatting ---------------------------------------------------------------------------------


def _venue_line(v: dict) -> str:
    rating = v.get("rating") or {}
    score = f" · ★{rating['score']}" if isinstance(rating, dict) and rating.get("score") else ""
    state = "" if v.get("online", True) else " · CLOSED"
    est = f" · {v['estimate_range']} min" if v.get("estimate_range") else ""
    price = " · " + "€" * int(v["price_range"]) if isinstance(v.get("price_range"), int) else ""
    tags = ", ".join(one_line(t, 20) for t in (v.get("tags") or [])[:4] if isinstance(t, str))
    return (f"{one_line(v.get('name'), 80)} [{v.get('slug')}]{state}{est}{score}{price}"
            + (f"\n  {one_line(v.get('address'), 80)}" if v.get("address") else "") + (f" · {tags}" if tags else ""))


def _required(item: dict) -> list[str]:
    out = []
    for c in item.get("options") or []:
        rng = ((c.get("multi_choice_config") or {}).get("total_range") or {}) if isinstance(c, dict) else {}
        if int(rng.get("min") or 0) >= 1:
            out.append(one_line(c.get("name"), 40))
    return out


def _item_line(it: dict, currency: str) -> str:
    req = _required(it)
    gone = " · NOT AVAILABLE" if it.get("disabled_info") else ""
    extra = f" · must choose: {', '.join(req)}" if req else (" · has extras" if it.get("options") else "")
    desc = one_line(it.get("description"), 140)
    return (f"[{it.get('id')}] {one_line(it.get('name'), 90)} — {money(it.get('price'), currency)}{gone}{extra}"
            + (f"\n    {desc}" if desc else ""))


def _wrap(s: str) -> str:
    """Names and descriptions are written by venues: data for the assistant, not instructions."""
    return f"‹‹‹ (from Wolt; written by the venues)\n{s}\n›››"


# --- reading ------------------------------------------------------------------------------------


async def wolt_venues(s: Services, a: dict) -> Result:
    w, near, limit = wo(s), a.get("lat_lng"), min(a.get("limit", 15), 50)
    if a.get("query"):
        data = await w.search(text(a["query"]), "venues", near)
        venues = [it["venue"] for sec in data.get("sections") or [] for it in sec.get("items") or []
                  if isinstance(it, dict) and isinstance(it.get("venue"), dict)]
    else:
        venues = await w.nearby(near)
    if a.get("open"):
        venues = [v for v in venues if v.get("online")]
    if not venues:
        return "No Wolt venues found" + (f" for {a['query']!r}" if a.get("query") else "") + " near there."
    more = f"\n(showing {limit} of {len(venues)})" if len(venues) > limit else ""
    return _wrap("\n".join(_venue_line(v) for v in venues[:limit])) + more


async def wolt_dishes(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    data = await wo(s).search(text(query), "items", a.get("lat_lng"))
    lines = []
    for sec in data.get("sections") or []:
        for it in sec.get("items") or []:
            m = it.get("menu_item") if isinstance(it, dict) else None
            if isinstance(m, dict):
                gone = "" if m.get("is_available", True) else " · not available"
                lines.append(f"[{m.get('id')}] {one_line(m.get('name'), 80)} — {money(m.get('price'), m.get('currency', 'EUR'))}"
                             f"{gone}\n  at {one_line(m.get('venue_name'), 60)} · {m.get('estimate_range', '?')} min")
    if not lines:
        return f"No dishes found for {query!r}."
    return _wrap("\n".join(lines[: min(a.get("limit", 20), 50)])) + \
        "\n(To see a venue's menu or order, use `wolt venues` to find its slug, then `wolt menu SLUG`.)"


async def wolt_venue(s: Services, a: dict) -> Result:
    (venue,) = need(a, "venue")
    w = wo(s)
    st = await w.venue_static(venue)
    v, raw = st.get("venue") or {}, st.get("venue_raw") or {}
    lines = [f"{one_line(v.get('name'), 80)} [{v.get('slug')}]",
             f"  {one_line(v.get('address'), 80)}, {one_line(v.get('city'), 40)} · {one_line(v.get('phone'), 30)}"]
    for d in v.get("opening_times_schedule") or []:
        if isinstance(d, dict):
            lines.append(f"  {one_line(d.get('day'), 12)}: {one_line(d.get('formatted_times'), 40)}")
    currency = str(v.get("currency") or "EUR")
    if st.get("order_minimum"):
        lines.append(f"  Minimum order: {money(st['order_minimum'], currency)}")
    fees = v.get("zero_distance_fees") or {}
    if fees.get("delivery_price") is not None:
        lines.append(f"  Delivery from {money(fees['delivery_price'], currency)} (more with distance)")
    if v.get("service_fee_short_description"):
        lines.append(f"  {one_line(v['service_fee_short_description'], 100)}")
    if w.near or a.get("lat_lng"):
        dyn = (await w.venue_dynamic(venue, a.get("lat_lng"))).get("venue") or {}
        status = (dyn.get("delivery_open_status") or dyn.get("open_status") or {})
        lines.append(f"  Now: {one_line(status.get('value'), 60) or ('online' if dyn.get('online') else 'offline')}")
        for c in dyn.get("delivery_configs") or []:
            if isinstance(c, dict) and c.get("method") == "homedelivery":
                lines.append(f"  Delivery to the owner's area: {one_line((c.get('estimate') or {}).get('label'), 30)}")
    url = share_url(v.get("share_url") or raw.get("share_url"))
    return _wrap("\n".join(lines)) + (f"\nOn Wolt: {url}" if url else "")


async def wolt_menu(s: Services, a: dict) -> Result:
    (venue,) = need(a, "venue")
    w = wo(s)
    if a.get("search"):
        data = await w.assortment_search(venue, a["search"])
    else:
        data = await w.assortment(venue)
    items = {i["id"]: i for i in data.get("items") or [] if isinstance(i, dict) and i.get("id")}
    currency = "EUR"
    if not items and data.get("loading_strategy") == "partial":
        cats = ", ".join(one_line(c.get("name"), 30) for c in (data.get("categories") or [])[:40])
        return (f"This is a shop with a big assortment; search it: `wolt menu {venue} --search TEXT`.\n"
                f"Departments: {cats}")
    if not items:
        return "Nothing found on that menu."
    out, listed = [], set()
    for c in data.get("categories") or []:
        ids = [i for i in c.get("item_ids") or [] if i in items and i not in listed]
        if not ids:
            continue
        out.append(f"## {one_line(c.get('name'), 60)}")
        for i in ids:
            listed.add(i)
            out.append(_item_line(items[i], currency))
    out += [_item_line(it, currency) for i, it in items.items() if i not in listed]
    return _wrap("\n".join(out)) + "\n(Choices for an item: `wolt item VENUE ITEM_ID`.)"


async def wolt_item(s: Services, a: dict) -> Result:
    venue, iid = need(a, "venue", "item")
    w = wo(s)
    data = await w.assortment(venue)
    items = {i["id"]: i for i in data.get("items") or [] if isinstance(i, dict)}
    options = {o["id"]: o for o in data.get("options") or [] if isinstance(o, dict)}
    if iid not in items:
        extra = await w.assortment_items(venue, [iid])
        items.update({i["id"]: i for i in extra.get("items") or [] if isinstance(i, dict)})
        options.update({o["id"]: o for o in extra.get("options") or [] if isinstance(o, dict)})
    it = items.get(iid)
    if not it:
        raise ToolError(f"item {iid} isn't on that menu")
    out = [_item_line(it, "EUR")]
    for c in it.get("options") or []:
        root = options.get(c.get("option_id")) or {}
        rng = (c.get("multi_choice_config") or {}).get("total_range") or {}
        lo, hi = int(rng.get("min") or 0), rng.get("max")
        rule = "required, choose 1" if lo == 1 and hi == 1 else f"choose {lo}-{hi}" if hi else f"at least {lo}"
        out.append(f"  Option {one_line(c.get('name') or root.get('name'), 60)!r} ({rule}):")
        for v in root.get("values") or []:
            out.append(f"    - {one_line(v.get('name'), 60)}" + (f" +{money(v['price'])}" if v.get("price") else ""))
    return _wrap("\n".join(out))


async def wolt_orders(s: Services, a: dict) -> Result:
    data = await wo(s).orders()
    orders = data.get("orders") or data.get("results") or data.get("items") or []
    lines = []
    for o in orders[: min(a.get("limit", 10), 20)]:
        if not isinstance(o, dict):
            continue
        venue = o.get("venue_name") or (o.get("venue") or {}).get("name")
        total = o.get("total_amount") or o.get("total_price") or o.get("payment_amount")
        lines.append(f"[{o.get('purchase_id') or o.get('id')}] {one_line(o.get('received_at') or o.get('created_at'), 25)}"
                     f" · {one_line(venue, 60)} · {money(total, o.get('currency', 'EUR'))} · {one_line(o.get('status'), 30)}")
    return _wrap("\n".join(lines)) if lines else "No Wolt orders found."


async def wolt_order(s: Services, a: dict) -> Result:
    (oid,) = need(a, "order_id")
    d = (await wo(s).order_status(oid)).get("order_details") or {}
    if not d:
        return "Wolt didn't return details for that order."
    cur = d.get("currency", "EUR")
    return (f"Order {oid}: {one_line(d.get('status'), 40)} · {one_line(d.get('delivery_method'), 20)} · "
            f"total {money(d.get('total_price') or d.get('payment_amount'), cur)}"
            + (f" (delivery {money(d['delivery_price'], cur)})" if d.get("delivery_price") is not None else ""))


async def wolt_status(s: Services, a: dict) -> Result:
    return f"Wolt: {wo(s).status()}."


# --- baskets ------------------------------------------------------------------------------------


async def _resolve_basket(s: Services, a: dict, replace: bool) -> dict:
    """Checks the lines against the live menu and fixes exactly what goes into the basket, so an approval shows
    what will happen. Runs before deciding whether to ask (callers can't set `resolved`)."""
    venue, wanted = need(a, "venue", "items")
    w = wo(s)
    if not w.connected:
        raise ToolError("filling a Wolt basket needs the owner's Wolt account (`ampi-tools wolt-login`); "
                        "until then, give the owner the venue link and the items")
    st = await w.venue_static(venue)
    v = st.get("venue") or {}
    if not isinstance(v.get("id"), str):
        raise ToolError("Wolt didn't return that venue")
    payload, lines, total = build_basket(await w.assortment(venue), v, wanted)
    baskets = await w.baskets()
    existing = v["id"] in json.dumps(baskets)
    if existing and not replace:
        raise ToolError("the owner already has a basket at this venue in Wolt; filling it would replace it. Use "
                        "`wolt basket --replace` (asks the owner first) or tell them")
    return {"resolved": {"venue": slug(venue), "venue_name": one_line(v.get("name"), 80), "payload": payload,
                         "lines": lines, "total": total, "currency": payload["currency"],
                         "minimum": st.get("order_minimum"), "existing": existing,
                         "url": share_url(v.get("share_url"))}}


async def _resolve_new(s: Services, a: dict) -> dict:
    return await _resolve_basket(s, a, replace=False)


async def _resolve_replace(s: Services, a: dict) -> dict:
    return await _resolve_basket(s, a, replace=True)


def _basket_text(r: dict) -> str:
    cur = r["currency"]
    out = [f"Wolt basket at {r['venue_name']}:"] + [f"  {line}" for line in r["lines"]]
    out.append(f"  Items: {money(r['total'], cur)} (+ delivery and service fee, shown at checkout)")
    if isinstance(r.get("minimum"), int) and r["total"] < r["minimum"]:
        out.append(f"  Below the {money(r['minimum'], cur)} minimum: Wolt adds a small-order fee")
    return "\n".join(out)


async def wolt_basket(s: Services, a: dict) -> Result:
    r = a.get("resolved")
    if not isinstance(r, dict):
        raise ToolError("internal: the basket wasn't checked first")
    saved = await wo(s).save_basket(r["payload"])
    if not isinstance(saved, dict) or not saved.get("id"):
        raise ToolError("Wolt didn't confirm the basket; the owner should check the Wolt app")
    return (_basket_text(r) + "\n✅ The basket is in the owner's Wolt account. Nothing is ordered or paid: they check "
            "out in the Wolt app" + (f" ({r['url']})" if r.get("url") else "") + ", where the final total is shown.")


async def describe_basket(s: Services, a: dict) -> str:
    r = a.get("resolved") or {}
    note = "REPLACES the basket you already have there. " if r.get("existing") else ""
    return (_basket_text(r) + f"\n{note}Nothing is ordered or paid: you check out in the Wolt app.")


WOLT_OPS = [
    Op("wolt.venues", False, wolt_venues), Op("wolt.dishes", False, wolt_dishes),
    Op("wolt.venue", False, wolt_venue), Op("wolt.menu", False, wolt_menu), Op("wolt.item", False, wolt_item),
    Op("wolt.orders", False, wolt_orders), Op("wolt.order", False, wolt_order), Op("wolt.status", False, wolt_status),
    Op("wolt.basket", False, wolt_basket, describe_basket, "Fill Wolt basket"),
    Op("wolt.basket_replace", True, wolt_basket, describe_basket, "REPLACE Wolt basket"),
]
OPS.update({op.name: op for op in WOLT_OPS})
PRE_RESOLVE.update({"wolt.basket": _resolve_new, "wolt.basket_replace": _resolve_replace})
