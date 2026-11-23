"""Egress firewall and small config files for the checkout browser (standard library only; runs as root).

  python3 -I checkout_net.py load    at boot (ampi-checkout-net.service): writes /etc/ampi-checkout/{resolv.conf,
                                     hosts,passwd,group} and loads the nft table `inet ampi_checkout`
  python3 -I checkout_net.py check   before the engine and the controller start (ExecStartPre=+): fails unless
                                     the table is loaded exactly as `load` left it

Separate users, so a hostile page can't drive the browser:
- `ampicheckout` runs the engine (Obscura, listening on 127.0.0.1:19333), and `ampicheckoutlp` the second one, if it is
  installed (Lightpanda, 127.0.0.1:19334, deploy/install-lightpanda.sh). Each may send DNS to the public resolvers
  and TCP 80/443 to public addresses that aren't this server's own, and replies on connections it accepted.
  Every other destination, every local address included, is rejected: page JavaScript can open
  ws://127.0.0.1:<port> (seen in the probe), so this rule is what keeps pages away from the debugger ports.
- `ampicheckoutctl` runs the controller; it alone (and root) may connect to those ports.
And against a squatter that binds one while its engine is down: packets *from* the port are dropped unless
that engine's user sent them (the controller also checks the listener's owner in /proc/net/tcp).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

try:  # deployed side by side in /usr/local/lib/ampi-checkout; in the package for tests
    from . import browser_net as bn
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import browser_net as bn  # type: ignore[no-redef]

ENGINE_USER = "ampicheckout"
CTL_USER = "ampicheckoutctl"
CDP_PORT = 19333
LP_USER, LP_PORT = "ampicheckoutlp", 19334
CONF_DIR = "/etc/ampi-checkout"
STATE = "/run/ampi-checkout-net.sha256"
TABLE = "ampi_checkout"


def engine_rules(u: int, port: int, ctl_uid: int, resolvers: list[str]) -> list[str]:
    r4 = [r for r in resolvers if ":" not in r]
    r6 = [r for r in resolvers if ":" in r]
    dns = []
    if r4:
        dns.append(f"\t\tmeta skuid {u} ip daddr {{ {', '.join(r4)} }} meta l4proto {{ tcp, udp }} th dport 53 accept")
    if r6:
        dns.append(f"\t\tmeta skuid {u} ip6 daddr {{ {', '.join(r6)} }} meta l4proto {{ tcp, udp }} th dport 53 accept")
    return [
        # The debugger port, for every user and both families: only the controller and root may connect.
        f"\t\ttcp dport {port} meta skuid != {{ 0, {ctl_uid} }} reject with tcp reset",
        # Anti-squatter: whatever answers from that port must be the engine's user.
        f"\t\ttcp sport {port} meta skuid != {u} drop",
        # The engine's answers to the controller (and to the sites it opened) on connections already allowed.
        f"\t\tmeta skuid {u} ct state established,related accept",
        *dns,
        f"\t\tmeta skuid {u} fib daddr type local reject with icmpx admin-prohibited",
        f"\t\tmeta skuid {u} ip daddr @deny4 reject with icmpx admin-prohibited",
        f"\t\tmeta skuid {u} ip6 daddr @deny6 reject with icmpx admin-prohibited",
        f"\t\tmeta skuid {u} tcp dport {{ 80, 443 }} ct state new limit rate over 600/minute burst 200 packets drop",
        f"\t\tmeta skuid {u} tcp dport {{ 80, 443 }} accept",
        f"\t\tmeta skuid {u} reject with icmpx admin-prohibited",
    ]


def ruleset(engine_uid: int, ctl_uid: int, own4: list[str], own6: list[str], resolvers: list[str],
            lp_uid: int | None = None) -> str:
    deny4 = bn._collapse(list(bn.DENY4) + own4)
    deny6 = bn._collapse(list(bn.DENY6) + own6)
    return "\n".join([
        f"table inet {TABLE} {{",
        "\tset deny4 {", "\t\ttype ipv4_addr", "\t\tflags interval", f"\t\telements = {{ {', '.join(deny4)} }}", "\t}",
        "\tset deny6 {", "\t\ttype ipv6_addr", "\t\tflags interval", f"\t\telements = {{ {', '.join(deny6)} }}", "\t}",
        "\tchain output {",
        "\t\ttype filter hook output priority 0; policy accept;",
        *engine_rules(engine_uid, CDP_PORT, ctl_uid, resolvers),
        *(engine_rules(lp_uid, LP_PORT, ctl_uid, resolvers) if lp_uid is not None else []),
        "\t}",
        "}",
    ]) + "\n"


def files(uid: int, gid: int, resolvers: list[str], lp: tuple[int, int] | None = None) -> dict[str, str]:
    """The engines' view of /etc: our resolvers, the browser's blocked names, and only their own user."""
    out = {
        "resolv.conf": "".join(f"nameserver {r}\n" for r in resolvers[:3]) + "options edns0\n",
        "hosts": "127.0.0.1 localhost\n::1 localhost\n" + "".join(f"0.0.0.0 {n}\n" for n in bn.BLOCKED_NAMES),
        "passwd": ("root:x:0:0:root:/root:/usr/sbin/nologin\n"
                   f"{ENGINE_USER}:x:{uid}:{gid}::/tmp/h:/usr/sbin/nologin\n"
                   "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"),
        "group": f"root:x:0:\n{ENGINE_USER}:x:{gid}:\nnogroup:x:65534:\n",
    }
    if lp is not None:
        out["passwd-lp"] = out["passwd"].replace(f"{ENGINE_USER}:x:{uid}:{gid}:", f"{LP_USER}:x:{lp[0]}:{lp[1]}:")
        out["group-lp"] = out["group"].replace(f"{ENGINE_USER}:x:{gid}:", f"{LP_USER}:x:{lp[1]}:")
    return out


def _nft(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(["/usr/sbin/nft", *args], input=stdin, capture_output=True, text=True, check=True).stdout


def fingerprint() -> str:
    data = json.loads(_nft("-n", "-y", "-j", "list", "table", "inet", TABLE))
    items = [i for i in data.get("nftables", []) if "metainfo" not in i]
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


def load() -> None:
    import pwd

    engine, ctl = pwd.getpwnam(ENGINE_USER), pwd.getpwnam(CTL_USER)
    try:
        lp = pwd.getpwnam(LP_USER)
    except KeyError:  # the second engine isn't installed
        lp = None
    resolvers = bn.read_resolvers(f"{CONF_DIR}/resolvers")
    own4, own6 = bn.own_networks(subprocess.run(["/usr/sbin/ip", "-j", "addr"], capture_output=True, text=True,
                                                check=True).stdout)
    os.makedirs(CONF_DIR, mode=0o755, exist_ok=True)
    for name, content in files(engine.pw_uid, engine.pw_gid, resolvers, (lp.pw_uid, lp.pw_gid) if lp else None).items():
        tmp = f"{CONF_DIR}/.{name}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, f"{CONF_DIR}/{name}")
    _nft("-f", "-", stdin=f"table inet {TABLE}\ndelete table inet {TABLE}\n"
                         + ruleset(engine.pw_uid, ctl.pw_uid, own4, own6, resolvers, lp.pw_uid if lp else None))
    with open(STATE, "w", encoding="utf-8") as f:
        f.write(fingerprint())
    print(f"ampi-checkout firewall loaded (own addresses denied: {', '.join(own4 + own6) or 'none'})")


def check() -> int:
    why = "the table differs from what was loaded"
    try:
        with open(STATE, encoding="utf-8") as f:
            expected = f.read().strip()
        ok = fingerprint() == expected
    except subprocess.CalledProcessError as e:
        ok, why = False, f"nft failed ({e.returncode}): {(e.stderr or '').strip()[:200]}"
    except (OSError, ValueError) as e:
        ok, why = False, f"{type(e).__name__}: {e}"[:200]
    if not ok:
        print(f"ampi-checkout: the firewall isn't loaded as expected ({why}); run systemctl restart ampi-checkout-net",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["load"]:
        load()
        sys.exit(0)
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    sys.exit("usage: checkout_net.py load|check")
