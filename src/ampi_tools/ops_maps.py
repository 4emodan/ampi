"""Google Maps operations (registered into ops.OPS when ops.py loads). All read-only; none needs approval.

Every answer ends with Google Maps sources (links), as the terms of Google's agent API require.
"""

from __future__ import annotations

from typing import Any

from .maps import (
    DAILY, Maps, TRAVEL_MODES, directions_link, distance, duration, maps_link, name_and_coords, one_line,
    place_text, safe_url, search_link, sources,
)
from .ops import FIELDS, OPS, Op, Result, Services, _bool, _choice, _date, _int, need
from .todoist import ToolError


def mp(s: Services) -> Maps:
    if s.maps_client is None:
        raise ToolError("Google Maps isn't set up")
    return s.maps_client


def _places(v: Any) -> list[str]:
    if not isinstance(v, list) or len(v) > 10:
        raise ToolError("at most 10 stops")
    return [place_text(x, "stop") for x in v]


def _links(v: Any) -> list[str]:
    if not isinstance(v, list) or not 1 <= len(v) <= 10:
        raise ToolError("give 1-10 Google Maps links")
    return list(dict.fromkeys(maps_link(x) for x in v))


def _when(v: Any) -> str:
    if not isinstance(v, str) or len(v) > 40:
        raise ToolError("time must be HH:MM or YYYY-MM-DD HH:MM")
    return v


FIELDS.update({
    "place": place_text, "origin": lambda v: place_text(v, "origin"),
    "destination": lambda v: place_text(v, "destination"), "near": lambda v: place_text(v, "near"),
    "via": _places, "links": _links, "mode": _choice(*TRAVEL_MODES), "depart": _when, "arrive": _when,
    "radius": _int(100, 50_000), "details": _bool, "air": _bool, "pollen": _bool, "alternatives": _bool,
    "hour": _int(0, 23), "date": _date,
})


def _place_lines(data: dict) -> list[tuple[str, str]]:
    out = []
    for p in data.get("places") or []:
        if not isinstance(p, dict):
            continue
        links = p.get("googleMapsLinks") or {}
        url = safe_url(links.get("placeUrl")) or safe_url((p.get("attribution") or {}).get("url"))
        title = (p.get("attribution") or {}).get("title") or "Google Maps"
        pid = str(p.get("id") or "")
        extra = f" (place_id:{pid})" if pid and len(pid) < 300 else ""
        reviews = safe_url(links.get("reviewsUrl"))
        out.append((f"{title}{extra}" + (f" · reviews: {reviews}" if reviews else ""), url))
    return out


async def maps_search(s: Services, a: dict) -> Result:
    (query,) = need(a, "query")
    query = place_text(query, "query")
    data = await mp(s).search(query, near=a.get("near"), radius=a.get("radius"), limit=min(a.get("limit", 5), 10),
                              details=a.get("details", False))
    summary = str(data.get("summary") or "").strip()
    places = _place_lines(data)
    if not summary and not places:
        return f"Google Maps found nothing for {query!r}."
    return f"{summary}\n\n{sources(places)}\nAll results on a map: {search_link(query)}"


async def maps_route(s: Services, a: dict) -> Result:
    origin, destination = need(a, "origin", "destination")
    mode = a.get("mode", "drive")
    data = await mp(s).route(origin, destination, mode=mode, via=a.get("via"), depart=a.get("depart"),
                             arrive=a.get("arrive"), alternatives=a.get("alternatives", False))
    routes = [r for r in data.get("routes") or [] if isinstance(r, dict)]
    link = directions_link(origin, destination, mode, a.get("via"))
    if not routes:
        return f"Google Maps found no {mode} route from {origin!r} to {destination!r}.\nOpen in Google Maps: {link}"
    lines = []
    for n, r in enumerate(routes[:3]):
        label = "Route" if len(routes) == 1 else f"Option {n + 1}"
        lines.append(f"{label}: {duration(r.get('duration'))}, {distance(r.get('distanceMeters'))}")
    when = f" (departing {a['depart']})" if a.get("depart") else f" (arriving by {a['arrive']})" if a.get("arrive") else ""
    head = f"{mode.capitalize()} from {origin} to {destination}{when}:"
    note = ""
    if mode == "transit":
        note = "\n(Google's agent API gives only the total time for transit; lines and changes are in the link.)"
    elif mode == "walk":
        note = "\n(Walking directions are in beta at Google and may miss sidewalks or paths.)"
    attr = [(str((r.get("attribution") or {}).get("title") or "Google Maps"),
             safe_url((r.get("attribution") or {}).get("url"))) for r in routes[:1]]
    return f"{head}\n" + "\n".join(lines) + note + f"\nOpen in Google Maps: {link}\n{sources(attr)}"


def _temp(t: Any) -> str:
    if not isinstance(t, dict) or t.get("degrees") is None:
        return "?"
    return f"{round(float(t['degrees']))}°C"


def _weather_block(w: dict) -> list[str]:
    cond = one_line(((w.get("weatherCondition") or {}).get("description") or {}).get("text"), 80)
    parts = []
    if w.get("temperature"):
        parts.append(_temp(w["temperature"]))
        if w.get("feelsLikeTemperature"):
            parts.append(f"feels like {_temp(w['feelsLikeTemperature'])}")
    if w.get("maxTemperature") or w.get("minTemperature"):
        parts.append(f"{_temp(w.get('minTemperature'))} to {_temp(w.get('maxTemperature'))}")
    prob = ((w.get("precipitation") or {}).get("probability") or {}).get("percent")
    if prob is not None:
        parts.append(f"{prob}% chance of precipitation")
    speed = ((w.get("wind") or {}).get("speed") or {}).get("value")
    if speed is not None:
        cardinal = ((w.get("wind") or {}).get("direction") or {}).get("cardinal", "")
        parts.append(f"wind {speed} km/h {str(cardinal).replace('_', ' ').lower()}".rstrip())
    for key, label in (("relativeHumidity", "humidity {}%"), ("uvIndex", "UV {}"), ("cloudCover", "clouds {}%")):
        if w.get(key) is not None:
            parts.append(label.format(w[key]))
    return [f"{cond}: " + ", ".join(parts) if cond else ", ".join(parts)]


async def maps_weather(s: Services, a: dict) -> Result:
    (place,) = need(a, "place")
    data = await mp(s).weather(place, date=a.get("date"), hour=a.get("hour"), air=a.get("air", False),
                               pollen=a.get("pollen", False))
    when = a.get("date", "now") + (f" {a['hour']:02d}:00" if a.get("hour") is not None else "")
    out = [f"Weather for {place} ({when}):"]
    attr: list[tuple[str, str]] = []
    w = data.get("weather") if isinstance(data.get("weather"), dict) else data
    out += ["  " + x for x in _weather_block(w)]
    a_ = (w.get("attribution") or data.get("attribution") or {})
    attr.append((str(a_.get("title") or "Google Maps"), safe_url(a_.get("url"))))
    aq = data.get("airQuality")
    if isinstance(aq, dict):
        for h in (aq.get("hourlyResults") or [])[:1]:
            for ix in (h.get("indexes") or [])[:2]:
                out.append(f"  Air quality: {one_line(ix.get('displayName'), 40)} {ix.get('aqi', ix.get('value', '?'))}"
                           f" ({one_line(ix.get('category'), 60)}), mostly {one_line(ix.get('dominantPollutant'), 20)}")
        attr.append((str((aq.get("attribution") or {}).get("title") or "Google Maps"),
                     safe_url((aq.get("attribution") or {}).get("url"))))
    pl = data.get("pollen")
    if isinstance(pl, dict):
        day = pl.get("dailyForecast") or {}
        for c in day.get("categories") or []:
            ix = c.get("index") or {}
            out.append(f"  Pollen, {one_line(c.get('displayName'), 30)}: {one_line(ix.get('category'), 30) or 'none'}"
                       + ("" if c.get("inSeason", True) else " (out of season)"))
        attr.append((str((pl.get("attribution") or {}).get("title") or "Google Maps"),
                     safe_url((pl.get("attribution") or {}).get("url"))))
    return "\n".join(out) + "\n" + sources([x for x in attr if x[1]] or [("Google Maps", "")])


async def maps_place(s: Services, a: dict) -> Result:
    """What a Google Maps link the owner sent points at: the place, then a short summary of it."""
    (links,) = need(a, "links")
    m = mp(s)
    resolved = await m.resolve_urls(links)
    entities = resolved.get("entities") or []
    failed = resolved.get("failedRequests") or {}
    out = []
    for n, link in enumerate(links):
        ent = entities[n] if n < len(entities) and isinstance(entities[n], dict) else {}
        pid = str(ent.get("place") or "").removeprefix("places/")
        if not pid or str(n) in failed:
            out.append(f"{link}: Google couldn't tell which place this is (only single-place links work).")
            continue
        try:
            expanded = await m.expand_short_link(link)
        except ToolError as e:
            expanded, note = link, f" ({e})"
        else:
            note = ""
        name, coords = name_and_coords(expanded)
        block = [f"{link} → place_id:{pid}" + (f" · {name}" if name else "") + note]
        if name:
            near = f"{coords['latitude']},{coords['longitude']}" if coords else None
            data = await m.search(name, near=near, radius=500 if coords else None, limit=3)
            match = [p for p in data.get("places") or [] if isinstance(p, dict) and p.get("id") == pid]
            if match and data.get("summary"):
                block.append(str(data["summary"]).strip())
                block.append(sources(_place_lines({"places": match})))
            else:
                block.append(f"(No summary: Google's search for {name!r} didn't return this exact place.)")
                block.append(sources([("Google Maps", search_link(name, pid))]))
        else:
            block.append(sources([("Google Maps", search_link(pid, pid))]))
        out.append("\n".join(block))
    return "\n\n".join(out)


async def maps_link_op(s: Services, a: dict) -> Result:
    """Keyless Google Maps links (works without MAPS_API_KEY)."""
    if a.get("origin") or a.get("destination"):
        origin, destination = need(a, "origin", "destination")
        return directions_link(origin, destination, a.get("mode", "drive"), a.get("via"))
    (query,) = need(a, "query")
    return search_link(place_text(query, "query"))


async def maps_status(s: Services, a: dict) -> Result:
    m = mp(s)
    return (f"Google Maps: {m.status()}; language {m.language}"
            + (f", region {m.region}" if m.region else "") + (", home area set" if m.near else "")
            + f", time zone {m.tz.key}.\nBudget: {m.budget.summary()} (daily caps {DAILY}).")


MAPS_OPS = [
    Op("maps.search", False, maps_search), Op("maps.route", False, maps_route),
    Op("maps.weather", False, maps_weather), Op("maps.place", False, maps_place),
    Op("maps.link", False, maps_link_op), Op("maps.status", False, maps_status),
]
OPS.update({op.name: op for op in MAPS_OPS})
