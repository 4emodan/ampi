"""The companion app's logic that has no Android in it, run with the JDK (skipped without one), and that its pairing code is
the server's."""

import shutil
import socket
import ssl
import struct
import subprocess
from pathlib import Path

import pytest

from ampi_phone import signin, trust

ROOT = Path(__file__).resolve().parent.parent / "companion"
JBR = Path("C:/Program Files/Android/Android Studio/jbr/bin")
JAVAC = shutil.which("javac") or (str(JBR / "javac.exe") if (JBR / "javac.exe").exists() else None)
OPENSSL = shutil.which("openssl")
JAVA = shutil.which("java") or (str(JBR / "java.exe") if (JBR / "java.exe").exists() else None)

pytestmark = pytest.mark.skipif(not (JAVAC and JAVA), reason="needs a JDK")


@pytest.fixture(scope="module")
def classes(tmp_path_factory):
    out = tmp_path_factory.mktemp("classes")
    src = ROOT / "src/app/ampi/companion"
    subprocess.run([JAVAC, "-encoding", "UTF-8", "--release", "17", "-d", str(out), *(str(src / f) for f in (
        "Frames.java", "AlarmTimes.java", "Texts.java", "Tls.java", "Hosts.java", "Totp.java", "SessionBox.java")), *map(str, (ROOT / "test").rglob("*.java"))], check=True)
    return out


def test_logic(classes):
    r = subprocess.run([JAVA, "-cp", str(classes), "app.ampi.companion.LogicTest"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_pairing_code_is_the_same_on_both_sides(classes):
    r = subprocess.run([JAVA, "-cp", str(classes), "app.ampi.companion.LogicTest", "code"], capture_output=True, text=True)
    assert r.stdout.splitlines()[:2] == [trust.pair_code(b"server", b"phone"), trust.pair_code(b"server", b"phone", b"key")]


def test_both_sides_refuse_the_same_sites(classes):
    sites = ["google.com", "accounts.google.com", "notgoogle.com", "x.com", "fox.com", "mybank.example", "bank.example", "example.bank", "a.pankki.example",
             "github.com", "gist.github.com", "club.example", "web.telegram.org"]
    r = subprocess.run([JAVA, "-cp", str(classes), "app.ampi.companion.LogicTest", "code", "x", *sites], capture_output=True, text=True)
    _, _, listed, *said = r.stdout.splitlines()
    assert set(listed.split()) == signin.NOT and "" not in listed.split(" ")
    assert [x == "true" for x in said] == [signin.refused(s) for s in sites]
    assert [s for s in sites if not signin.refused(s)] == ["notgoogle.com", "fox.com", "example.bank", "club.example"]


def cert(folder, name):
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "30",
                    "-subj", f"/CN={name}", "-keyout", f"{name}.key", "-out", f"{name}.crt"], cwd=folder, check=True,
                   capture_output=True)
    subprocess.run([OPENSSL, "pkcs12", "-export", "-inkey", f"{name}.key", "-in", f"{name}.crt", "-out", f"{name}.p12",
                    "-passout", "pass:test"], cwd=folder, check=True, capture_output=True)
    return ssl.PEM_cert_to_DER_cert((folder / f"{name}.crt").read_text())


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    if OPENSSL is None:
        pytest.skip("needs openssl")
    d = tmp_path_factory.mktemp("tls")
    return d, {n: cert(d, n) for n in ("server", "phone", "other")}


def channel(classes, certs, *, pinned="server", phone="phone", talk=None):
    """The app's TLS client against ampi-signin's kind of server; what each side saw."""
    d, ders = certs
    (d / "devices").mkdir(exist_ok=True)
    ctx = trust.make_context([trust.Device("phone", ders["phone"])], d)
    with socket.create_server(("127.0.0.1", 0)) as s:
        s.settimeout(30)
        p = subprocess.Popen([JAVA, "-cp", str(classes), "app.ampi.companion.TlsTest", str(s.getsockname()[1]),
                              str(d / f"{phone}.p12"), str(d / f"{pinned}.crt")], stdout=subprocess.PIPE, text=True)
        conn, _ = s.accept()
        conn.settimeout(30)
        try:
            with ctx.wrap_socket(conn, server_side=True) as t:
                assert t.getpeercert(True) == ders["phone"] and t.version() == "TLSv1.3"
                saw = talk(t) if talk else t.recv(1)
        except (ssl.SSLError, OSError) as e:
            saw = type(e).__name__
            conn.close()
    return saw, p.communicate(timeout=30)[0].strip()


def test_the_apps_tls_talks_to_pythons(classes, certs):
    def talk(t):
        out = []
        for text in ("{}", "é" * 20000, "x" * 65536):
            b = text.encode()
            t.sendall(struct.pack(">I", len(b)) + b)
            want = struct.pack(">I", len(b)) + b
            got = b""
            while len(got) < len(want) and (part := t.recv(65536)):
                got += part
            out.append(got == want)
        t.unwrap()
        return out

    assert channel(classes, certs, talk=talk) == ([True, True, True], "goodbye")


def test_a_cut_channel_is_not_a_goodbye(classes, certs):
    def talk(t):
        t.sendall(struct.pack(">I", 5) + b"ab")
        sock = t.detach()
        socket.socket(fileno=sock).close()

    assert channel(classes, certs, talk=talk)[1].startswith("error")


def test_the_phone_refuses_another_server(classes, certs):
    saw, said = channel(classes, certs, pinned="other")
    assert saw in ("SSLError", "SSLCertVerificationError", "ConnectionResetError") and said.startswith("error")


def test_the_service_refuses_another_phone(classes, certs):
    saw, said = channel(classes, certs, phone="other")
    assert saw == "SSLCertVerificationError" and said.startswith("error")