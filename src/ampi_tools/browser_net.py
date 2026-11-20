"""Egress firewall and small config files for ampi-browser (standard library only; runs as root).

  python3 -I browser_net.py load    at boot (ampi-browser-net.service): writes /etc/ampi-browser/{resolv.conf,
                                    hosts,passwd,group} and loads the nft table `inet ampi_browser`
  python3 -I browser_net.py check   before every browser instance (ExecStartPre=+): fails unless the table
                                    is loaded exactly as `load` left it, so the browser never runs unfirewalled

The table only touches packets sent by the `ampibrowser` user: DNS to the configured public resolvers, and
TCP 80/443 to public addresses that aren't this server's own. Everything else it sends is dropped.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import subprocess
import sys

USER = "ampibrowser"
CONF_DIR = "/etc/ampi-browser"
STATE = "/run/ampi-browser-net.sha256"
TABLE = "ampi_browser"
DEFAULT_RESOLVERS = ("185.12.64.1", "185.12.64.2", "2a01:4ff:ff00::add:1", "2a01:4ff:ff00::add:2")
DENY4 = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
         "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
         "203.0.113.0/24", "224.0.0.0/3")
DENY6 = ("::/96","::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32", "2001:db8::/32",
         "2002::/16", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")
# YouTube is only reached through the youtube command (official API/feeds); never via the browser.
BLOCKED_NAMES = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
                 "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com", "youtubei.googleapis.com",
                 "youtube.googleapis.com", "gaming.youtube.com", "tv.youtube.com")


def own_networks(ip_json: str) -> tuple[list[str], list[str]]:
    """This server's own global addresses (IPv4 as /32, IPv6 as its /64), from `ip -j addr`."""
    v4, v6 = [], []
    for link in json.loads(ip_json or "[]"):
        for a in link.get("addr_info", []):
            try:
                ip = ipaddress.ip_address(a.get("local", ""))
            except ValueError:
                continue
            if not ip.is_global:
                continue
            if ip.version == 4:
                v4.append(f"{ip}/32")
            else:
                v6.append(str(ipaddress.ip_network(f"{ip}/64", strict=False)))
    return v4, v6


def _collapse(nets) -> list[str]:
    return [str(n) for n in ipaddress.collapse_addresses(ipaddress.ip_network(n) for n in nets)]


def read_resolvers(path: str = f"{CONF_DIR}/resolvers") -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        lines = list(DEFAULT_RESOLVERS)
    out = []
    for ln in lines:
        ip = ipaddress.ip_address(ln)  # a bad line fails loudly
        if not ip.is_global:
            raise SystemExit(f"resolver {ln} isn't a public address")
        out.append(str(ip))
    return out


def ruleset(uid: int, own4: list[str], own6: list[str], resolvers: list[str]) -> str:
    deny4 = _collapse(list(DENY4) + own4)
    deny6 = _collapse(list(DENY6) + own6)
    r4 = [r for r in resolvers if ipaddress.ip_address(r).version == 4]
    r6 = [r for r in resolvers if ipaddress.ip_address(r).version == 6]
    dns = []
    if r4:
        dns.append(f"\t\tmeta skuid {uid} ip daddr {{ {', '.join(r4)} }} meta l4proto {{ tcp, udp }} th dport 53 accept")
    if r6:
        dns.append(f"\t\tmeta skuid {uid} ip6 daddr {{ {', '.join(r6)} }} meta l4proto {{ tcp, udp }} th dport 53 accept")
    return "\n".join([
        f"table inet {TABLE} {{",
        "\tset deny4 {", "\t\ttype ipv4_addr", "\t\tflags interval", f"\t\telements = {{ {', '.join(deny4)} }}", "\t}",
        "\tset deny6 {", "\t\ttype ipv6_addr", "\t\tflags interval", f"\t\telements = {{ {', '.join(deny6)} }}", "\t}",
        "\tchain output {",
        "\t\ttype filter hook output priority 0; policy accept;",
        *dns,
        # Any address of this machine, including ones added after boot (the sets below are a snapshot).
        f"\t\tmeta skuid {uid} fib daddr type local drop",
        f"\t\tmeta skuid {uid} ip daddr @deny4 drop",
        f"\t\tmeta skuid {uid} ip6 daddr @deny6 drop",
        f"\t\tmeta skuid {uid} tcp dport {{ 80, 443 }} ct state new limit rate over 600/minute burst 200 packets drop",
        f"\t\tmeta skuid {uid} tcp dport {{ 80, 443 }} accept",
        f"\t\tmeta skuid {uid} drop",
        "\t}",
        "}",
    ]) + "\n"


def files(uid: int, gid: int, resolvers: list[str]) -> dict[str, str]:
    return {
        "resolv.conf": "".join(f"nameserver {r}\n" for r in resolvers[:3]) + "options edns0\n",
        "hosts": "127.0.0.1 localhost\n::1 localhost\n" + "".join(f"0.0.0.0 {n}\n" for n in BLOCKED_NAMES),
        "passwd": ("root:x:0:0:root:/root:/usr/sbin/nologin\n"
                   f"{USER}:x:{uid}:{gid}::/tmp/h:/usr/sbin/nologin\n"
                   "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"),
        "group": f"root:x:0:\n{USER}:x:{gid}:\nnogroup:x:65534:\n",
    }


def _nft(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["/usr/sbin/nft", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def fingerprint() -> str:
    data = json.loads(_nft("-j", "list", "table", "inet", TABLE))
    # Leave out nft's own version info, so a package upgrade doesn't look like a changed firewall.
    items = [i for i in data.get("nftables", []) if "metainfo" not in i]
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def load() -> None:
    import pwd  # POSIX only; the rest of this module is importable anywhere (tests)

    user = pwd.getpwnam(USER)
    resolvers = read_resolvers()
    own4, own6 = own_networks(subprocess.run(["/usr/sbin/ip", "-j", "addr"], capture_output=True, text=True,
                                             check=True).stdout)
    os.makedirs(CONF_DIR, mode=0o755, exist_ok=True)
    for name, content in files(user.pw_uid, user.pw_gid, resolvers).items():
        tmp = f"{CONF_DIR}/.{name}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, f"{CONF_DIR}/{name}")
    # Declaring the table first makes the delete safe on the first run; the whole file is one transaction.
    _nft("-f", "-", stdin=f"table inet {TABLE}\ndelete table inet {TABLE}\n"
                         + ruleset(user.pw_uid, own4, own6, resolvers))
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(fingerprint())
    print(f"ampi-browser firewall loaded (own addresses denied: {', '.join(own4 + own6) or 'none'})")


def check() -> int:
    try:
        with open(STATE, encoding="utf-8") as f:
            expected = f.read().strip()
        ok = fingerprint() == expected
    except (OSError, subprocess.CalledProcessError, ValueError):
        ok = False
    if not ok:
        print("ampi-browser: the egress firewall isn't loaded as expected; "
              "run systemctl restart ampi-browser-net", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["load"]:
        load()
        sys.exit(0)
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    sys.exit("usage: browser_net.py load|check")
