"""The hub: the address book API, its access checks and the vault edits behind it (over TCP here; the real server
listens on a Unix socket)."""

from __future__ import annotations

import http.client
import json
import socketserver
import threading

import pytest

from ampi_tools import hub as hubmod
from ampi_tools.vault import Conflict, Vault, entry_rev

TOKEN = "t" * 43


@pytest.fixture
def served(tmp_path):
    calls = []

    def geocode(address):
        calls.append(address)
        return {"location": "35.100000,33.300000", "title": "Somewhere", "url": "https://maps.google.com/?cid=1"}

    hub = hubmod.Hub(tmp_path, token=TOKEN, geocode=geocode)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), hub.handler())
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], hub, calls, tmp_path
    server.shutdown()
    server.server_close()


def req(port, method, path, body=None, headers=None, token=TOKEN, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{port}"}
    if token is not None:
        h["X-Ampi-Hub"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw), r
    except ValueError:
        return r.status, raw.decode(), r


GYM = {"name": "gym", "address": {"line1": "Gym Street 5", "city": "Nicosia", "country": "cy", "postal": "1010"},
       "location": "35.1,33.3", "timezone": "Europe/Nicosia", "search": True, "revealable": False}


def test_pages_are_static_and_need_no_token(served):
    port, *_ = served
    status, body, r = req(port, "GET", "/address-book", token=None)
    assert status == 200 and "Address book" in body
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Cache-Control") == "no-store" and r.getheader("X-Frame-Options") == "DENY"
    assert req(port, "GET", "/hub.js", token=None)[0] == 200
    assert req(port, "GET", "/../hub.py", token=None)[0] == 404
    assert req(port, "GET", "/vault.json", token=None)[0] == 404


def test_api_needs_the_token(served):
    port, *_ = served
    assert req(port, "GET", "/api/places", token=None)[0] == 401
    assert req(port, "GET", "/api/places", token="x" * 43)[0] == 401
    assert req(port, "GET", "/api/places")[0] == 200


def test_other_hosts_and_cross_site_requests_are_refused(served):
    port, *_ = served
    assert req(port, "GET", "/api/places", host="evil.example:8765")[0] == 421  # DNS rebinding
    assert req(port, "GET", "/address-book", host="evil.example", token=None)[0] == 421
    assert req(port, "GET", "/api/places", host=f"localhost:{port}")[0] == 200
    assert req(port, "POST", "/api/places", GYM, headers={"Origin": "http://evil.example"})[0] == 403
    assert req(port, "POST", "/api/places", GYM, headers={"Origin": "http://127.0.0.1:9999"})[0] == 403
    assert req(port, "POST", "/api/places", GYM, headers={"Sec-Fetch-Site": "same-site"})[0] == 403
    assert req(port, "GET", "/api/places", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    ok = req(port, "POST", "/api/places", GYM, headers={"Origin": f"http://127.0.0.1:{port}",
                                                         "Sec-Fetch-Site": "same-origin"})
    assert ok[0] == 201


def test_writes_need_json(served):
    port, *_ = served
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/api/places", body="name=gym", headers={"Host": f"127.0.0.1:{port}", "X-Ampi-Hub": TOKEN,
                                                                  "Content-Type": "application/x-www-form-urlencoded"})
    assert conn.getresponse().status == 415
    conn.close()
    big = {**GYM, "company": "x" * (70 * 1024)}
    assert req(port, "POST", "/api/places", big)[0] == 413


def test_create_edit_rename_delete(served):
    port, hub, _, home = served
    status, body, _ = req(port, "POST", "/api/places", GYM)
    assert status == 201
    gym = body["place"]
    assert gym["address"]["country"] == "CY" and gym["search"] is True and gym["revealable"] is False
    assert req(port, "POST", "/api/places", GYM)[0] == 409  # create never replaces
    # A whole replace: the cleared postal code goes, nothing merged from before.
    change = {**GYM, "address": {**GYM["address"], "postal": ""}, "company": "Fit Ltd", "rev": gym["rev"]}
    status, body, _ = req(port, "PUT", "/api/places/gym", change)
    assert status == 200 and body["place"]["address"]["postal"] == "" and body["place"]["company"] == "Fit Ltd"
    # The old rev is stale now.
    assert req(port, "PUT", "/api/places/gym", {**change, "company": "Other"})[0] == 409
    new_rev = body["place"]["rev"]
    status, body, _ = req(port, "PUT", "/api/places/gym", {**change, "name": "gym-old", "rev": new_rev})
    assert status == 200 and body["place"]["name"] == "gym-old"
    assert Vault(home).entry("gym") is None
    assert req(port, "DELETE", "/api/places/gym-old", {"rev": "nope"})[0] == 409
    assert req(port, "DELETE", "/api/places/gym-old", {"rev": body["place"]["rev"]})[0] == 200
    assert req(port, "GET", "/api/places")[1]["places"] == []


def test_only_places_are_touched(served):
    port, hub, _, home = served
    v = Vault(home)
    v.set_entry("me", "person", {"name": "Al Owner", "email": "a@example.com"})
    v.set_entry("secret", "note", {"text": "do not show"})
    places = req(port, "GET", "/api/places")[1]["places"]
    assert places == []
    assert req(port, "PUT", "/api/places/me", {**GYM, "name": "me", "rev": "x"})[0] == 404
    assert req(port, "DELETE", "/api/places/secret", {"rev": "x"})[0] == 404
    assert req(port, "POST", "/api/places", {**GYM, "name": "me"})[0] == 409
    assert req(port, "POST", "/api/places", {**GYM, "name": "Bad.Name"})[0] == 400
    assert req(port, "POST", "/api/places", {**GYM, "name": "card"})[0] == 400  # reserved
    # A rename onto another entry's name is refused.
    req(port, "POST", "/api/places", GYM)
    rev = req(port, "GET", "/api/places")[1]["places"][0]["rev"]
    assert req(port, "PUT", "/api/places/gym", {**GYM, "name": "me", "rev": rev})[0] == 409
    assert Vault(home).entry("me")["type"] == "person"


def test_bad_fields(served):
    port, *_ = served
    assert req(port, "POST", "/api/places", {**GYM, "address": {**GYM["address"], "evil": "x"}})[0] == 400
    assert req(port, "POST", "/api/places", {**GYM, "search": "yes"})[0] == 400
    assert req(port, "POST", "/api/places", {**GYM, "location": "not a place"})[0] == 400
    assert req(port, "POST", "/api/places", {**GYM, "timezone": "Mars/Base"})[0] == 400
    assert req(port, "POST", "/api/places", {"name": "empty"})[0] == 400
    assert req(port, "POST", "/api/places", ["not", "an", "object"])[0] == 400


def test_geocode_and_its_cap(served, monkeypatch):
    port, hub, calls, _ = served
    status, body, _ = req(port, "POST", "/api/geocode", {"address": "Gym Street 5, Nicosia"})
    assert status == 200 and body["location"] == "35.100000,33.300000"
    assert calls == ["Gym Street 5, Nicosia"]
    hub.book._geo_count = hubmod.GEOCODES_PER_DAY
    assert req(port, "POST", "/api/geocode", {"address": "Gym Street 5, Nicosia"})[0] == 429
    assert req(port, "POST", "/api/geocode", {"address": "x"})[0] == 400


def test_geocode_without_a_key(tmp_path):
    book = hubmod.AddressBook(Vault(tmp_path), None)
    with pytest.raises(hubmod.HttpError) as e:
        book.geocode({"address": "Gym Street 5"})
    assert e.value.status == 503


def test_flags_rules(tmp_path):
    v = Vault(tmp_path)
    v.replace_entry("work", "place", {"address": {"line1": "A 1", "city": "B", "country": "CY"}},
                    flags={"search": False, "revealable": False}, create=True)
    e = v.entry("work")
    assert e["search"] is False and e["revealable"] is False and e["created"]


def test_rename_drops_old_grants(tmp_path):
    v = Vault(tmp_path)
    v.set_entry("work", "place", {"address": {"line1": "A 1", "city": "B", "country": "CY"}})
    v.grant("https://shop.example", ["work.address.line1"])
    v.grant("https://other.example", ["me.email"])
    e = v.entry("work")
    v.replace_entry("work", "place", e["fields"], new_name="office", rev=entry_rev({k: x for k, x in e.items() if k != "id"}))
    assert [g["origin"] for g in v.grants()] == ["https://other.example"]


def test_stale_rev_is_a_conflict(tmp_path):
    v = Vault(tmp_path)
    v.set_entry("work", "place", {"address": {"line1": "A 1", "city": "B", "country": "CY"}})
    with pytest.raises(Conflict):
        v.replace_entry("work", "place", {"timezone": "UTC"}, rev="0" * 16)
    with pytest.raises(Conflict):
        v.delete("work", rev="0" * 16)
    assert v.entry("work")


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="needs fcntl (Linux)")
def test_concurrent_writers_keep_both_changes(tmp_path):
    a, b = Vault(tmp_path), Vault(tmp_path)

    def add(v, name):
        v.set_entry(name, "note", {"text": name})

    threads = [threading.Thread(target=add, args=(a if i % 2 else b, f"n{i}")) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names = {e["name"] for e in Vault(tmp_path).entries()}
    assert names == {f"n{i}" for i in range(10)}


def test_token_comes_from_the_file_and_rotates(tmp_path):
    f = tmp_path / "token"
    hub = hubmod.Hub(tmp_path, token_file=f)
    assert hub.current_token() == ""  # no file: nothing is accepted
    f.write_text("a" * 43 + "\n")
    assert hub.current_token() == "a" * 43
    f.write_text("b" * 44 + "\n")  # the helper writes a new one (another size/mtime)
    assert hub.current_token() == "b" * 44
    f.write_text("short\n")
    assert hub.current_token() == ""
    f.unlink()
    assert hub.current_token() == ""


def test_errors_close_the_connection(served):
    port, *_ = served
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    # A body the server never reads (401 before it) must not be parsed as a second request.
    smuggled = b"GET /api/places HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    conn.request("POST", "/api/places", body=smuggled, headers={"Host": f"127.0.0.1:{port}",
                                                                 "Content-Type": "text/plain"})
    r = conn.getresponse()
    assert r.status == 401 and r.getheader("Connection") == "close"
    r.read()
    conn.close()


def test_log_lines_hold_no_values(served, caplog):
    port, *_ = served
    caplog.set_level("INFO", logger="ampi-hub")
    req(port, "POST", "/api/places", GYM)
    req(port, "POST", "/api/geocode", {"address": "Secret Street 9"})
    text = caplog.text
    assert "Gym Street" not in text and "Secret Street" not in text and "35.1" not in text and TOKEN not in text
    assert "gym" in text
