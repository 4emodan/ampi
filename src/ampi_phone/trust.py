"""The paired phones and the TLS settings (used by the gateway, the sign-in service and root's `ampi-phone` command).

/etc/ampi-phone (root:ampiphone, 0750): config.json {"host", "port"}, server.key, server.crt and devices/NAME.json
{"name", "cert" (base64 DER), "added", "code", "signin"}. /etc/ampi-signin (root:ampisignin, 0750) has the same without
config.json: its own key, and the phones that were paired with it. Only root writes there.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

CONF = Path("/etc/ampi-phone")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
MAX_CERT = 4096


@dataclass(frozen=True)
class Device:
    name: str
    der: bytes


def parse_cert(b64: str) -> bytes:
    """A phone's certificate as sent by the app (base64 DER); raises ValueError."""
    if not isinstance(b64, str) or len(b64) > MAX_CERT * 2:
        raise ValueError("the certificate is too long")
    try:
        der = base64.b64decode("".join(b64.split()), validate=True)
    except binascii.Error:
        raise ValueError("the certificate isn't base64") from None
    if not 100 < len(der) <= MAX_CERT:
        raise ValueError("that isn't a certificate")
    try:
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.load_verify_locations(cadata=der)
    except (ssl.SSLError, ValueError):
        raise ValueError("that isn't a certificate") from None
    # Exactly one certificate, nothing after it.
    if der[:2] != b"\x30\x82" or int.from_bytes(der[2:4], "big") + 4 != len(der):
        raise ValueError("that isn't exactly one certificate")
    return der


def load_devices(conf: Path = CONF) -> list[Device]:
    out = []
    for f in sorted((conf / "devices").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if not NAME_RE.fullmatch(str(d.get("name"))) or f.name != f"{d['name']}.json":
            raise ValueError(f"{f}: bad device file")
        out.append(Device(d["name"], parse_cert(d["cert"])))
    if len({d.der for d in out}) != len(out):
        raise ValueError("two devices share a certificate")
    return out


def server_der(conf: Path = CONF) -> bytes:
    return ssl.PEM_cert_to_DER_cert((conf / "server.crt").read_text(encoding="ascii"))


def load_config(conf: Path = CONF) -> dict:
    c = json.loads((conf / "config.json").read_text(encoding="utf-8"))
    if not isinstance(c.get("host"), str) or not isinstance(c.get("port"), int):
        raise ValueError("config.json needs host and port")
    return c


def make_context(devices: list[Device], conf: Path = CONF) -> ssl.SSLContext:
    """TLS 1.3 only; a client certificate is required and must be one of the paired phones' own. Not
    create_default_context(): newer Pythons add X509_STRICT, which refuses the phone's plain certificates."""
    if not devices:
        raise ValueError("no paired phone")
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    c.minimum_version = ssl.TLSVersion.TLSv1_3
    c.verify_mode = ssl.CERT_REQUIRED
    # Each phone's certificate is its own trust anchor (and two with the same subject don't get in each other's way).
    c.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    c.num_tickets = 0  # no resumption: every connection shows its certificate
    c.load_cert_chain(conf / "server.crt", conf / "server.key")
    c.load_verify_locations(cadata=b"".join(d.der for d in devices))
    return c


def pair_code(server: bytes, phone: bytes, signin: bytes = b"", version: int = 2) -> str:
    """The code both sides show: 130 bits over everything pairing fixes, the sign-in service's certificate included.
    (Version 1: phones paired before there was one.)"""
    text = f"ampi-pair-v{version}\n" + "\n".join(hashlib.sha256(x).hexdigest() if x else "-" for x in (server, phone, signin)) + "\n"
    code = base64.b32encode(hashlib.sha256(text.encode()).digest()).decode()[:26]
    return " ".join([code[:5], code[5:10], code[10:15], code[15:20], code[20:26]])


def same_code(a: str, b: str) -> bool:
    return "".join(a.split()).upper() == "".join(b.split()).upper()
