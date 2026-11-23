"""The phone gateway: real TLS over loopback with throwaway certificates (needs the openssl command)."""

import asyncio
import base64
import json
import shutil
import socket
import ssl
import struct
import subprocess
import time

import pytest

from ampi_phone import admin, gateway, trust

OPENSSL = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(OPENSSL is None, reason="needs openssl")


def make_cert(folder, name, subject="/CN=test"):
    """A self-signed certificate without extensions that matter, like the Android Keystore's."""
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "30",
                    "-subj", subject, "-keyout", f"{name}.key", "-out", f"{name}.crt"], cwd=folder, check=True,
                   capture_output=True)
    return ssl.PEM_cert_to_DER_cert((folder / f"{name}.crt").read_text())


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if OPENSSL is None:
        pytest.skip("needs openssl")
    d = tmp_path_factory.mktemp("certs")
    return d, {"server": make_cert(d, "server", "/CN=ampi-phone"), "a": make_cert(d, "a"), "b": make_cert(d, "b"),
               "stranger": make_cert(d, "stranger")}


@pytest.fixture
def conf(certs, tmp_path):
    d, ders = certs
    c = tmp_path / "conf"
    (c / "devices").mkdir(parents=True)
    for f in ("server.key", "server.crt"):
        (c / f).write_bytes((d / f).read_bytes())
    (c / "config.json").write_text('{"host": "192.0.2.1", "port": 28471}')
    return c, ders


@pytest.fixture(autouse=True)
def no_systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(admin, "run", lambda *cmd, check=True: calls.append(cmd))
    monkeypatch.setattr(admin.time, "sleep", lambda s: None)
    return calls


def pair(c, name, der):
    (c / "devices" / f"{name}.json").write_text(json.dumps({"name": name, "cert": base64.b64encode(der).decode(), "added": "x"}))


class Phone:
    """The app's side of the connection."""

    def __init__(self, folder, cert, server_der):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.server_der = server_der
        if cert:
            self.ctx.load_cert_chain(folder / f"{cert}.crt", folder / f"{cert}.key")

    async def connect(self, port, hello=True):
        self.rd, self.wr = await asyncio.open_connection("127.0.0.1", port, ssl=self.ctx)
        assert self.wr.get_extra_info("ssl_object").getpeercert(True) == self.server_der
        assert self.wr.get_extra_info("ssl_object").version() == "TLSv1.3"
        w = await self.read()
        if hello:
            await self.send({"t": "hello", "app": "0.1", "sdk": "36.0", "zone": "Europe/Helsinki", "warn": ["a‮b", 5]})
        return w

    async def read(self):
        n = struct.unpack(">I", await asyncio.wait_for(self.rd.readexactly(4), 5))[0]
        return json.loads(await self.rd.readexactly(n))

    async def send(self, msg):
        self.wr.write(gateway.frame(msg))
        await self.wr.drain()

    async def answer(self, **kw):
        m = await self.read()
        await self.send({"t": "res", "id": m["id"], "ok": True, "data": {}, **kw})
        return m


class Running:
    def __init__(self, conf):
        self.gw = gateway.Gateway(conf)

    async def __aenter__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.task = asyncio.create_task(self.gw.accept(self.sock))
        return self

    async def __aexit__(self, *a):
        self.task.cancel()
        for link in list(self.gw.links.values()):
            link.abort()
        for t in list(self.gw._tasks):
            t.cancel()
        await asyncio.gather(*self.gw._tasks, return_exceptions=True)
        self.sock.close()

    async def ready(self, device="a"):
        for _ in range(200):
            link = self.gw.links.get(device)
            if link is not None and link.hello:
                return link
            await asyncio.sleep(0.01)
        raise AssertionError("not connected")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def refused(conf, folder, cert, server):
    async def go():
        async with Running(conf) as r:
            with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, OSError)):
                await Phone(folder, cert, server).connect(r.port)
            assert not r.gw.links
    run(go())


def test_a_paired_phone_connects_and_answers(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            w = await p.connect(r.port)
            assert w["t"] == "welcome" and w["proto"] == 1 and abs(w["now"] - time.time()) < 5
            await r.ready()
            ask = asyncio.create_task(r.gw.control({"cmd": "ask", "kind": "notify", "body": {"title": "x"}}))
            m = await p.answer(data={"warn": []})
            assert m["t"] == "req" and m["kind"] == "notify" and m["body"] == {"title": "x"}
            assert 5 < m["exp"] - time.time() <= gateway.WAIT
            resp = await ask
            assert resp["ok"] and resp["answer"] == {"ok": True, "data": {"warn": []}, "error": None}
            assert resp["hello"]["warn"] == ["a‮b"] and resp["hello"]["zone"] == "Europe/Helsinki"
            st = (await r.gw.control({"cmd": "status"}))["devices"][0]
            assert st["connected"] and st["name"] == "a"
    run(go())


@pytest.mark.parametrize("cert", [None, "stranger"])
def test_nobody_else_gets_in(conf, certs, cert):
    c, ders = conf
    pair(c, "a", ders["a"])
    refused(c, certs[0], cert, ders["server"])


def test_no_paired_phone_means_no_handshake(conf, certs):
    refused(conf[0], certs[0], "a", conf[1]["server"])


@pytest.mark.parametrize("who", ["a", "b"])
def test_two_phones_with_the_same_subject_both_work(conf, certs, who):
    c, ders = conf
    pair(c, "a", ders["a"])
    pair(c, "b", ders["b"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], who, ders["server"])
            await p.connect(r.port)
            await r.ready(who)
    run(go())


def test_a_removed_phone_is_refused(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])
    pair(c, "b", ders["b"])
    admin.remove(c, "a")
    refused(c, certs[0], "a", ders["server"])


def test_a_new_connection_replaces_the_old_one(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p1, p2 = Phone(certs[0], "a", ders["server"]), Phone(certs[0], "a", ders["server"])
            await p1.connect(r.port)
            first = await r.ready()
            await p2.connect(r.port)
            for _ in range(100):
                if r.gw.links.get("a") not in (None, first):
                    break
                await asyncio.sleep(0.01)
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError, OSError)):
                await p1.read()
            await asyncio.sleep(0.05)
            assert r.gw.links["a"] is not first and r.gw.links["a"].hello  # the old one's cleanup left the new one alone
    run(go())


def test_a_waiting_request_is_sent_again_on_the_new_connection(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p1 = Phone(certs[0], "a", ders["server"])
            await p1.connect(r.port)
            await r.ready()
            ask = asyncio.create_task(r.gw.ask("alarm.list", {}))
            m1 = await p1.read()
            p1.wr.transport.abort()
            p2 = Phone(certs[0], "a", ders["server"])
            await p2.connect(r.port)
            m2 = await p2.answer(data={"alarms": []})
            assert m2["id"] == m1["id"] and m2["exp"] == m1["exp"]
            assert (await ask)["ok"]
            assert not r.gw.pending
    run(go())


def test_no_answer_is_unknown_and_drops_the_connection(conf, certs, monkeypatch):
    c, ders = conf
    pair(c, "a", ders["a"])
    monkeypatch.setattr(gateway, "WAIT", 0.2)

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            await p.connect(r.port)
            await r.ready()
            assert await r.gw.ask("alarm.set", {}) == {"ok": False, "code": "unknown"}
            await asyncio.sleep(0.05)
            assert not r.gw.links and not r.gw.pending
            off = await r.gw.ask("status", {})
            assert off["code"] == "offline" and off["paired"] and abs(off["last_seen"] - time.time()) < 5
    run(go())


def test_not_ready_before_hello(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            await p.connect(r.port, hello=False)
            assert (await r.gw.ask("status", {}))["code"] == "offline"
            await p.send({"t": "res", "id": "1", "ok": True})
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError, OSError)):
                await p.read()
    run(go())


@pytest.mark.parametrize("raw", [struct.pack(">I", 0), struct.pack(">I", gateway.MAX_FRAME + 1), struct.pack(">I", 3) + b"[1]",
                                 struct.pack(">I", 2) + b"\xff\xff", struct.pack(">I", 1) + b"{"])
def test_malformed_frames_close_the_connection(conf, certs, raw):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            await p.connect(r.port)
            await r.ready()
            p.wr.write(raw)
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError, OSError)):
                await p.read()
            await asyncio.sleep(0.05)
            assert not r.gw.links
    run(go())


def test_limits_on_requests(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            await p.connect(r.port)
            await r.ready()
            for _ in range(gateway.PER_MINUTE):
                ask = asyncio.create_task(r.gw.ask("status", {}))
                await p.answer()
                assert (await ask)["ok"]
            assert (await r.gw.ask("status", {}))["code"] == "limit"
    run(go())


@needs_openssl
def test_control_refuses_what_it_does_not_know(conf):
    gw = gateway.Gateway(conf[0])
    for req in (None, [], {"cmd": "ask", "kind": "vault.unlock", "body": {}}, {"cmd": "ask", "kind": "notify", "body": "x"},
                {"cmd": "ask", "kind": "notify", "body": {"text": "x" * 9000}}, {"cmd": "shell"}):
        assert run(gw.control(req))["ok"] is False
    assert run(gw.control({"cmd": "ask", "kind": "status", "body": {}})) == {
        "ok": False, "code": "offline", "last_seen": None, "paired": False}


def test_gate_limits_handshakes():
    t = [0.0]
    g = gateway.Gate(lambda: t[0])
    assert g.enter("1.2.3.4") and g.enter("1.2.3.4") and not g.enter("1.2.3.4") and g.enter("5.6.7.8")
    g.leave("1.2.3.4")
    assert g.enter("1.2.3.4")
    for k in ("1.2.3.4", "1.2.3.4", "5.6.7.8"):
        g.leave(k)
    assert g.total == 0 and not g.running
    assert g.key("127.0.0.1") == g.key("127.0.9.9") == g.key("::1") == g.key("::ffff:127.0.0.1") == "local"
    for _ in range(gateway.ADDR_PER_MINUTE):
        if g.enter("9.9.9.9"):
            g.leave("9.9.9.9")
    assert not g.enter("9.9.9.9")
    t[0] = 61
    assert g.enter("9.9.9.9")
    assert g.key("2001:db8:1:2:3::4") == g.key("2001:db8:1:2:ffff::1") != g.key("2001:db8:1:3::1")
    assert g.key("::ffff:1.2.3.4") == g.key("1.2.3.4") and g.key("nonsense") == "?"


def test_strangers_cannot_take_every_slot():
    t = [0.0]
    g = gateway.Gate(lambda: t[0])
    g.authenticated("7.7.7.7")
    taken = sum(g.enter(f"10.0.{i}.1") for i in range(200))
    assert taken == gateway.HANDSHAKES - gateway.RESERVED
    assert not g.enter("8.8.8.8") and sum(g.enter("7.7.7.7") for _ in range(20)) == gateway.KNOWN_PER_ADDR
    t[0] = gateway.KNOWN_S + 1
    assert not g.enter("7.7.7.7")  # a day later it is a stranger again
    for i in range(500):
        g.authenticated(f"1.1.{i % 250}.{i}")
    assert len(g.known) == gateway.KNOWN_MAX
    g.authenticated("local")
    assert not g._is_known("local")


def test_a_paired_phones_address_is_remembered(conf, certs):
    c, ders = conf
    pair(c, "a", ders["a"])

    async def go():
        async with Running(c) as r:
            p = Phone(certs[0], "a", ders["server"])
            await p.connect(r.port)
            await r.ready()
            assert "local" in r.gw.gate.known and r.gw.gate.total == 0
    run(go())


def test_hello_is_cut_to_a_known_shape():
    h = gateway.clean_hello({"app": 5, "model": "x" * 500 + "\x00", "warn": "no", "evil": 1})
    assert h == {"app": "", "sdk": "", "model": "x" * 60, "zone": "", "warn": []}


# --- pairing ------------------------------------------------------------------------------------


def test_pairing_needs_the_matching_code(conf):
    c, ders = conf
    code = trust.pair_code(ders["server"], ders["a"])
    assert len(code.replace(" ", "")) == 26
    with pytest.raises(ValueError, match="code"):
        admin.add(c, "phone", ders["b"], code)
    assert not trust.load_devices(c)
    admin.add(c, "phone", ders["a"], code.lower())
    assert [d.name for d in trust.load_devices(c)] == ["phone"]
    with pytest.raises(ValueError, match="already"):
        admin.add(c, "phone", ders["b"], trust.pair_code(ders["server"], ders["b"]))
    with pytest.raises(ValueError, match="already"):
        admin.add(c, "phone2", ders["a"], code)
    for name in ("../x", "A", "", "-x"):
        with pytest.raises(ValueError):
            admin.add(c, name, ders["b"], trust.pair_code(ders["server"], ders["b"]))
        with pytest.raises(ValueError):
            admin.remove(c, name)


def test_pairing_that_does_not_start_is_undone(conf, monkeypatch):
    c, ders = conf

    def fails(*cmd, check=True):
        if cmd[:2] == ("systemctl", "restart"):
            raise subprocess.CalledProcessError(1, cmd, stderr="no")
    monkeypatch.setattr(admin, "run", fails)
    with pytest.raises(subprocess.CalledProcessError):
        admin.add(c, "phone", ders["a"], trust.pair_code(ders["server"], ders["a"]))
    assert not trust.load_devices(c)


def test_removing_disconnects_first(conf, no_systemctl):
    c, ders = conf
    pair(c, "a", ders["a"])
    (c / "devices" / "bad.json").write_text("{")
    with pytest.raises(ValueError):  # another broken file: reported, but the lost phone is already disconnected and gone
        admin.remove(c, "a")
    assert no_systemctl[0] == ("systemctl", "stop", "ampi-phone.service") and not (c / "devices" / "a.json").exists()
    (c / "devices" / "bad.json").unlink()
    admin.apply(c)
    assert ("systemctl", "disable", "--now", "ampi-phone.socket") in no_systemctl


def test_the_code_covers_both_certificates_and_the_signer_slot():
    codes = {trust.pair_code(b"s", b"p"), trust.pair_code(b"p", b"s"), trust.pair_code(b"s", b"q"), trust.pair_code(b"s", b"p", b"k")}
    assert len(codes) == 4


def test_only_single_certificates_are_accepted(conf):
    der = conf[1]["a"]
    assert trust.parse_cert(base64.b64encode(der).decode() + "\n") == der
    for bad in ("", "!!", base64.b64encode(b"x" * 300).decode(), base64.b64encode(der + conf[1]["b"]).decode(),
                base64.b64encode(der + b"\0").decode(), "A" * 20000):
        with pytest.raises(ValueError):
            trust.parse_cert(bad)


def test_offer_has_only_public_data(conf, capsys, monkeypatch):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0, raising=False)
    assert admin.main(["offer"], conf[0]) == 0
    offer = json.loads(capsys.readouterr().out)
    assert set(offer) == {"v", "host", "port", "cert"} and base64.b64decode(offer["cert"]) == conf[1]["server"]


def test_a_broken_device_file_stops_the_gateway(conf):
    c, ders = conf
    (c / "devices" / "x.json").write_text(json.dumps({"name": "y", "cert": base64.b64encode(ders["a"]).decode()}))
    with pytest.raises(ValueError):
        gateway.Gateway(c)
