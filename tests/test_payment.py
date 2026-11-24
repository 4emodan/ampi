import asyncio
import json
import os
import stat
import sys
from datetime import date
from decimal import Decimal

import pytest

from ampi_tools.ops import Approvals, Policy, Services, validate
from ampi_tools.ops_checkout import CheckoutSide, PaymentRules, field_matches, grand_total, render
from ampi_tools.server import Daemon
from ampi_tools.todoist import ToolError
from ampi_tools.vault import Vault, check_card, has_card_number, luhn_ok, mask_text

CARD = "4242424242424242"
PROFILE = {"name": "Anna Virtanen", "email": "anna.virtanen@example.fi", "phone": "+358 40 1234567",
           "address": {"line1": "Mannerheimintie 1 A 2", "postal": "00100", "city": "Helsinki", "country": "fi"}}
SHOP = "https://shop.example"
STRIPE = "https://js.stripe.com"


def run(coro):
    return asyncio.run(coro)


class FakeCtl:
    """Stands in for the checkout controller (checkout.py) on its socket."""

    def __init__(self):
        self.calls: list[dict] = []
        self.session = "s1"
        self.state = "active"
        self.origin = SHOP
        self.totals = [{"amount": "23.90", "currency": "EUR", "label": "Total"}]
        self.risky: list[str] = []
        self.text = "Checkout. Deliver to Anna Virtanen, anna.virtanen@example.fi"
        self.fail_pay = None
        self.gate_totals = []
        self.form = [{"label": "Email", "kind": "input", "value": "anna.virtanen@example.fi", "shown": True},
                     {"label": "10:00", "kind": "button", "value": True, "shown": True},
                     {"label": "promo", "kind": "input", "value": "x", "shown": False}]
        self.elements = {
            "f0:4": {"ref": "f0:4", "frame": "f0", "frame_origin": SHOP, "tag": "input", "type": "email",
                     "autocomplete": "email", "name": "email", "id": "email", "label": "Email"},
            "f0:5": {"ref": "f0:5", "frame": "f0", "frame_origin": SHOP, "tag": "input", "type": "text",
                     "autocomplete": "name", "name": "name", "id": "name", "label": "Full name"},
            "f0:8": {"ref": "f0:8", "frame": "f0", "frame_origin": SHOP, "tag": "input", "type": "text",
                     "autocomplete": "", "name": "q", "id": "search", "label": "Search"},
            "f0:9": {"ref": "f0:9", "frame": "f0", "frame_origin": SHOP, "tag": "input", "type": "text",
                     "autocomplete": "cc-number", "name": "cardnumber", "id": "cn", "label": "Card number"},
            "f2:3": {"ref": "f2:3", "frame": "f2", "frame_origin": STRIPE, "tag": "input", "type": "text",
                     "autocomplete": "cc-number", "name": "cardnumber", "id": "", "label": "Card number"},
            "f2:5": {"ref": "f2:5", "frame": "f2", "frame_origin": STRIPE, "tag": "input", "type": "text",
                     "autocomplete": "cc-exp", "name": "exp-date", "id": "", "label": "MM / YY"},
            "f2:7": {"ref": "f2:7", "frame": "f2", "frame_origin": STRIPE, "tag": "input", "type": "text",
                     "autocomplete": "cc-csc", "name": "cvc", "id": "", "label": "CVC"},
            "f3:1": {"ref": "f3:1", "frame": "f3", "frame_origin": "https://evil.example", "tag": "input",
                     "type": "text", "autocomplete": "cc-number", "name": "cardnumber", "id": "", "label": "Card"},
            "f0:20": {"ref": "f0:20", "frame": "f0", "frame_origin": SHOP, "tag": "button", "type": "submit",
                      "autocomplete": "", "name": "", "id": "pay", "label": "Pay 23.90 €"},
        }

    def snapshot(self):
        return {"url": SHOP + "/checkout", "origin": self.origin, "title": "Checkout", "text": self.text,
                "elements": list(self.elements.values()), "totals": self.totals, "risky_checked": self.risky,
                "captcha": False, "threeds": False, "readonly": self.state == "readonly"}

    async def call(self, req, timeout=None):
        self.calls.append(req)
        cmd = req["cmd"]
        if cmd == "status":
            return {"ok": True, "session": self.session, "state": self.state}
        if cmd == "bind":
            return {"ok": True, "origin": self.origin, "totals": self.totals, "risky_checked": self.risky,
                    "bindings": [self.elements[r] for r in req["refs"] if r in self.elements]}
        if cmd == "gate":
            e = self.elements[req["ref"]]
            return {"ok": True, "kind": "link" if e["tag"] == "a" else "button", "frame_origin": e["frame_origin"], "tag": e["tag"],
                    "filled": getattr(self, "filled", False), "texts": e.get("texts", [e["label"]] if e["label"] else []),
                    "ident": e["id"], "submits": e.get("posts", False), "search": e.get("search", False), "dest": e.get("dest", ""),
                    "href": SHOP + "/page", "totals": self.gate_totals, "priced": getattr(self, "priced", False), "frames": getattr(self, "frames", [SHOP])}
        if cmd == "bind_submit":
            e = self.elements[req["ref"]]
            return {"ok": True, "origin": self.origin, "url": self.origin + "/book", "button": e,
                    "totals": self.totals, "risky_checked": self.risky, "captcha": getattr(self, "captcha", False),
                    "title": "Booking", "form": self.form, "digest": "d1", "gen": 3, "around": "Fri 10:00 selected", "seen": "s1", "priced": getattr(self, "priced", False),
                    "frames": getattr(self, "frames", [SHOP]),
                    "press": {"tag": e.get("tag", "button"), "texts": [e["label"]], "dest": getattr(self, "dest", SHOP + "/b"), "method": "post",
                              "submits": True}, **getattr(self, "box", {})}
        if cmd == "submit":
            if self.fail_pay:
                raise self.fail_pay
            return {"ok": True, "session": self.session, "snapshot": {**self.snapshot(), "text": "Booking confirmed"}}
        if cmd == "pay":
            if self.fail_pay:
                raise ToolError(self.fail_pay)
            self.state = "readonly"
            return {"ok": True, "session": self.session, "snapshot": {**self.snapshot(),
                    "title": "Thank you", "text": f"Order 1234 paid with card {CARD[:4]} {CARD[4:8]} {CARD[8:12]} {CARD[12:]}"}}
        if cmd == "hold":
            return {"ok": True}
        return {"ok": True, "session": self.session, "snapshot": self.snapshot()}


def make(tmp_path, **rules):
    v = Vault(tmp_path)
    v.set_profile(PROFILE)
    v.set_card({"number": CARD, "exp_month": 12, "exp_year": 2030, "cvc": "123", "name": "Anna Virtanen"})
    ctl = FakeCtl()
    side = CheckoutSide(ctl, v, PaymentRules(**rules) if rules else PaymentRules())
    s = Services(checkout_side=side)
    d = Daemon(s, Approvals(tmp_path / "a.sqlite3"), Policy(), lambda pid: asyncio.sleep(0, True))
    return d, side, ctl


def req(op, **args):
    return validate([{"op": op, "args": args}])


def approve(d, res, always=False):
    row = d.approvals.get(res["approval"])
    return run(d.decide(res["approval"], True, row["nonce"], None, always=always))


# --- vault -------------------------------------------------------------------------------------


def test_luhn_and_card_like_runs():
    assert luhn_ok(CARD) and not luhn_ok("4242424242424241")
    assert has_card_number(f"pay {CARD[:4]} {CARD[4:8]}-{CARD[8:12]}.{CARD[12:]} now")
    assert not has_card_number("order 1234567890123")


def test_card_checks():
    with pytest.raises(ToolError):
        check_card({"number": "1234", "exp_month": 1, "exp_year": 30, "cvc": "1", "name": "x"})
    with pytest.raises(ToolError, match="expired"):
        check_card({"number": CARD, "exp_month": 1, "exp_year": 2020, "cvc": "123", "name": "x"}, today=date(2026, 11, 1))


def test_vault_file_private_and_masked(tmp_path):
    d, side, _ = make(tmp_path)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(tmp_path / "vault.json").st_mode) == 0o600
    shown = "\n".join(side.vault.masked())
    assert "Visa •••• 4242" in shown and CARD not in shown and "anna.virtanen" not in shown and "123" not in shown


def test_mask_text_catches_spaced_card_and_values():
    out = mask_text(f"Card 4242 4242-4242.4242, Anna Virtanen", ["Anna Virtanen"], CARD)
    assert CARD[:4] not in out and "Anna" not in out and out.count("[vault]") == 2


# --- personal info ------------------------------------------------------------------------------


def test_reveal_asks_then_collect_once(tmp_path):
    d, side, _ = make(tmp_path)
    res = run(d.run(req("info.reveal", fields=["email", "name"]), "for the booking form", origin="private"))
    assert res["approval"] and "anna" not in res["text"].lower()
    out = approve(d, res)
    assert out["status"] == "done" and "anna" not in out["text"].lower()  # nothing in the owner's chat log either
    got = run(d.run(req("info.collect", approval=res["approval"]), None, origin="private"))
    assert "email: anna.virtanen@example.fi" in got["text"]
    again = run(d.run(req("info.collect", approval=res["approval"]), None, origin="private"))
    assert not again["ok"]


def test_card_is_never_revealable():
    with pytest.raises(ToolError, match="never"):
        validate([{"op": "info.reveal", "args": {"fields": ["card.number"]}}])


@pytest.mark.parametrize("origin", ["group:family", "watcher:inbox", "forwarded:private", "unknown"])
def test_only_the_owners_chat(tmp_path, origin):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="only for the owner's own chat"):
        run(d.run(req("checkout.snapshot"), None, origin=origin))
    with pytest.raises(ToolError):
        run(d.run(req("info.reveal", fields=["email"]), None, origin=origin))


# --- checkout fill ------------------------------------------------------------------------------


def test_fill_asks_then_site_grant_skips_asking(tmp_path):
    d, side, ctl = make(tmp_path)
    fills = [{"ref": "f0:4", "field": "email"}, {"ref": "f0:5", "field": "name"}]
    res = run(d.run(req("checkout.fill", fills=fills), None, origin="private"))
    assert res["approval"] and "Fill your me.email, me.name on https://shop.example" in res["text"]
    assert "anna" not in res["text"].lower()
    assert any(c["cmd"] == "hold" for c in ctl.calls)
    pending = d.approvals.unnotified()[0]
    assert "fill me.email, me.name on https://shop.example without asking" in pending["grant_label"]
    out = approve(d, res, always=True)
    assert out["status"] == "done" and "doesn't ask" in out["text"]
    fill = [c for c in ctl.calls if c["cmd"] == "fill"][-1]
    assert {f["value"] for f in fill["fields"]} == {"anna.virtanen@example.fi", "Anna Virtanen"}
    # Now granted: the same fields on the same site go straight in
    res2 = run(d.run(req("checkout.fill", fills=fills), None, origin="private"))
    assert res2["ok"] and "approval" not in res2


def test_grant_is_for_the_exact_site(tmp_path):
    d, side, ctl = make(tmp_path)
    side.vault.grant(SHOP, ["email"])
    ctl.origin = "https://shop.example.evil.com"
    ctl.elements["f0:4"]["frame_origin"] = ctl.origin
    res = run(d.run(req("checkout.fill", fills=[{"ref": "f0:4", "field": "email"}]), None, origin="private"))
    assert res.get("approval")


def test_fill_refuses_wrong_kind_of_field(tmp_path):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="doesn't look like a me.email"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f0:8", "field": "email"}]), None, origin="private"))


def test_fill_never_takes_card_fields(tmp_path):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="only by `checkout pay`"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f2:3", "field": "card.number"}]), None, origin="private"))


def test_snapshot_masks_even_where_filled_or_granted(tmp_path):
    d, side, ctl = make(tmp_path)
    res = run(d.run(req("checkout.snapshot"), None, origin="private"))
    assert "Anna" not in res["text"] and "anna.virtanen" not in res["text"] and "[vault]" in res["text"]
    side.vault.grant(SHOP, ["email"])
    res = run(d.run(req("checkout.snapshot"), None, origin="private"))
    # A granted site can't echo the value back to Ampi.
    assert "anna.virtanen@example.fi" not in res["text"] and "Anna Virtanen" not in res["text"]


# --- pay ----------------------------------------------------------------------------------------

PAY = dict(fills=[{"ref": "f2:3", "field": "card.number"}, {"ref": "f2:5", "field": "card.exp"},
                  {"ref": "f2:7", "field": "card.cvc"}],
           button="f0:20", amount="23.90", currency="EUR", what="2 × Pad thai")


def test_pay_asks_with_an_honest_summary_and_pays_after_approval(tmp_path):
    d, side, ctl = make(tmp_path)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    assert res["approval"]
    s = res["text"]
    assert "PAY 23.90 EUR at https://shop.example" in s and "Visa •••• 4242" in s and "into https://js.stripe.com" in s
    assert CARD not in s and "123" not in s.replace("1234", "")
    assert not d.approvals.unnotified()[0].get("grant_label")  # never an "always" for payments
    out = approve(d, res)
    assert out["status"] == "done" and "Pressed pay" in out["text"]
    assert CARD[:4] + " " not in out["text"] and "[vault]" in out["text"]
    pay = [c for c in ctl.calls if c["cmd"] == "pay"][-1]
    assert {f["value"] for f in pay["fields"]} == {CARD, "12/30", "123"} and all(f["card"] for f in pay["fields"])
    assert pay["expect_totals"] == [{"amount": "23.90", "currency": "EUR"}]
    assert side.vault.purchases()[0]["status"] == "submitted" and side.vault.purchases()[0]["card"].startswith("Visa")


def test_pay_never_offers_always(tmp_path):
    d, _, _ = make(tmp_path)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    row = d.approvals.get(res["approval"])
    with pytest.raises(ToolError, match="no"):
        run(d.decide(res["approval"], True, row["nonce"], None, always=True))


@pytest.mark.parametrize("change,err", [
    (lambda c: setattr(c, "totals", [{"amount": "99.00", "currency": "EUR", "label": "Total"}]), "page's total is 99.00"),
    (lambda c: setattr(c, "risky", ["Save my card for later"]), "subscription or cost"),
])
def test_pay_refusals_from_the_page(tmp_path, change, err):
    d, _, ctl = make(tmp_path)
    change(ctl)
    with pytest.raises(ToolError, match=err):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))


def test_card_only_into_payment_provider_frames(tmp_path):
    d, _, _ = make(tmp_path)
    for ref in ("f0:9", "f3:1"):  # the merchant's own page field, and an unknown frame
        bad = {**PAY, "fills": [{"ref": ref, "field": "card.number"}]}
        with pytest.raises(ToolError, match="payment provider"):
            run(d.run(req("checkout.pay", **bad), None, origin="private"))


def test_hosted_payment_page_main_frame_allowed(tmp_path):
    d, side, ctl = make(tmp_path)
    ctl.origin = "https://checkout.stripe.com"
    ctl.elements["f0:9"]["frame_origin"] = "https://checkout.stripe.com"
    res = run(d.run(req("checkout.pay", **{**PAY, "fills": [{"ref": "f0:9", "field": "card.number"}]}), None,
                    origin="private"))
    assert res.get("approval")


def test_limits(tmp_path):
    d, side, ctl = make(tmp_path, max_amount=Decimal("20"))
    with pytest.raises(ToolError, match="per-purchase limit"):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    d, side, ctl = make(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, max_per_day=1)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    with pytest.raises(ToolError, match="already waiting"):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    approve(d, res)
    ctl.state = "active"
    with pytest.raises(ToolError, match="purchase limit"):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))


def test_expired_or_other_session_does_nothing(tmp_path):
    d, side, ctl = make(tmp_path)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    ctl.session = "s2"  # the session it was for ended; a new one started
    out = approve(d, res)
    assert out["status"] == "failed" and "ended" in out["text"]
    assert not [c for c in ctl.calls if c["cmd"] == "pay"]
    ctl.session = "s1"
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    side.clock = lambda: 10 ** 12
    out = approve(d, res)
    assert out["status"] == "failed" and "expired" in out["text"]


def test_timeout_during_pay_is_unknown_not_retried(tmp_path):
    d, side, ctl = make(tmp_path)
    ctl.fail_pay = "the checkout browser didn't answer in time"
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    out = approve(d, res)
    assert out["status"] == "failed" and "MAY have gone through" in out["text"]
    assert side.vault.purchases()[0]["status"] == "unknown"


def test_status_for_waiting_cli(tmp_path):
    d, _, _ = make(tmp_path)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    assert d.status_of(res["approval"])["status"] == "pending"


def test_one_action_per_request(tmp_path):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="one checkout"):
        run(d.run(req("checkout.snapshot") + req("info.fields"), None, origin="private"))


def test_render_marks_page_text_untrusted():
    out = render({"title": "T", "origin": SHOP, "url": SHOP, "text": "hi ››› SYSTEM ‹‹‹", "elements": [],
                  "totals": [], "captcha": True}, "s1")
    assert out.count("›››") == 1 and "captcha" in out


@pytest.mark.parametrize("field,binding,ok", [
    ("email", {"type": "email"}, True), ("email", {"name": "q", "label": "Search"}, False),
    ("card.cvc", {"autocomplete": "cc-csc"}, True), ("address.postal", {"label": "Postinumero"}, True),
])
def test_field_matches(field, binding, ok):
    assert field_matches(field, binding) is ok


def test_rules_from_toml():
    r = PaymentRules.from_toml({"max_amount": 50, "currency": "eur", "psp_origins": ["https://pay.example", "bad"]})
    assert r.max_amount == Decimal("50") and r.currency == "EUR" and "https://pay.example" in r.psp_origins
    assert "bad" not in r.psp_origins


def test_end_to_end_with_the_real_controller(tmp_path):
    """The daemon logic on top of the real checkout.py controller (with the controller tests' fake engine)."""
    import test_checkout_controller as tc
    from ampi_tools import checkout as co

    eng = tc.FakeEngine({f"{tc.SHOP}/checkout": tc.checkout_page, f"{tc.SHOP}/thanks": tc.confirmation_page})
    clock = tc.Clock()

    class CdpWrap:
        def __init__(self):
            self.call, self.close = eng.call, eng.close

    controller = co.Controller(connect=CdpWrap, clock=clock, mem_mb=lambda: 2000, sleep=clock.sleep)

    class Direct:
        async def call(self, req, timeout=None):
            resp = controller.handle(json.loads(json.dumps(req)))
            if not resp.get("ok"):
                raise ToolError(f"checkout: {resp.get('error')}")
            return resp

    d, side, _ = make(tmp_path)
    side.client = Direct()
    side.rules = PaymentRules(psp_origins=(tc.PSP,))
    out = run(d.run(req("checkout.open", url=f"{tc.SHOP}/checkout"), None, origin="private"))
    assert out["ok"], out
    snap = controller.handle({"cmd": "snapshot", "session": controller.handle({"cmd": "status"})["session"]})["snapshot"]
    refs = {e["label"]: e["ref"] for e in snap["elements"]}
    res = run(d.run(req("checkout.fill", fills=[{"ref": refs["Email"], "field": "email"}]), None, origin="private"))
    assert approve(d, res)["status"] == "done"
    pay = dict(fills=[{"ref": refs["Card number"], "field": "card.number"}, {"ref": refs["MM / YY"], "field": "card.exp"},
                      {"ref": refs["CVC"], "field": "card.cvc"}],
               button=refs["Pay 23,90 €"], amount="23.90", currency="EUR", what="Pad thai")
    with pytest.raises(ToolError, match="payment provider"):  # the merchant's own card box is refused
        run(d.run(req("checkout.pay", **{**pay, "fills": [{"ref": refs["Card number (merchant)"],
                                                            "field": "card.number"}]}), None, origin="private"))
    res = run(d.run(req("checkout.pay", **pay), None, origin="private"))
    out = approve(d, res)
    assert out["status"] == "done", out
    assert controller.handle({"cmd": "status"})["state"] == "readonly"
    texts = json.dumps([r["result"] for r in d.approvals.recent(10)] + [out["text"]])
    assert CARD not in texts and "anna.virtanen" not in texts


def test_stated_amount_must_be_the_grand_total(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.totals = [{"amount": "9.00", "currency": "EUR", "label": "Subtotal"},
                  {"amount": "109.00", "currency": "EUR", "label": "Total"},
                  {"amount": "0.00", "currency": "EUR", "label": "Total incl. VAT 0.00"}]
    with pytest.raises(ToolError, match="page's total is 109.00"):
        run(d.run(req("checkout.pay", **{**PAY, "amount": "9.00"}), None, origin="private"))
    with pytest.raises(ToolError, match="per-purchase limit"):
        run(d.run(req("checkout.pay", **{**PAY, "amount": "109.00"}), None, origin="private"))


def test_the_engine_reaches_the_controller_only_when_chosen(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.session = None
    run(d.run(req("checkout.open", url=f"{SHOP}/x", engine="lightpanda"), None, origin="private"))
    run(d.run(req("checkout.open", url=f"{SHOP}/x"), None, origin="private"))
    a, b = [c for c in ctl.calls if c["cmd"] == "open"]
    assert a["engine"] == "lightpanda" and "engine" not in b
    with pytest.raises(ToolError, match="obscura or lightpanda"):
        req("checkout.open", url=f"{SHOP}/x", engine="chrome")


def test_no_card_payments_in_the_second_engine(tmp_path):
    d, _, ctl = make(tmp_path)
    real = ctl.call

    async def call(rq, timeout=None):
        r = await real(rq, timeout)
        return {**r, "engine": "lightpanda"} if rq["cmd"] == "status" else r
    ctl.call = call
    with pytest.raises(ToolError, match="default engine"):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    assert not [c for c in ctl.calls if c["cmd"] in ("bind", "pay")]


def test_the_command_line_passes_the_engine(monkeypatch):
    from ampi_tools import client
    seen = []
    monkeypatch.setattr(client, "_run_waiting", lambda who, ops, *a: seen.append(ops) or 0)
    client.checkout_main(["open", "https://x.example/", "--engine", "lightpanda"])
    client.checkout_main(["open", "https://x.example/"])
    a, b = ([o["args"] for o in (x if isinstance(x, list) else [x])][0] for x in seen)
    assert a["engine"] == "lightpanda" and not b.get("engine")


def test_a_total_that_cannot_be_read_is_not_guessed(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.totals = [{"amount": "23.90", "currency": "EUR", "label": "total"},
                  {"amount": "unreadable (1,23,456)", "currency": "EUR", "label": "grand total"}]
    with pytest.raises(ToolError, match="no order total in EUR found.*unreadable"):
        run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    ctl.totals[1]["label"] = "subtotal"  # (not what is charged)
    assert run(d.run(req("checkout.pay", **PAY), None, origin="private")).get("approval")


@pytest.mark.parametrize("text,total", [
    ("Subtotal €90.00\nTotal €99.00", "99.00"), ("Total incl. VAT: €99.00", "99.00"), ("Total (incl. €20.00 VAT) €99.00", "99.00"),
    ("Yhteensä (sis. ALV 24%) 99,00 €", "99.00"), ("Gesamtsumme 12 €\nGesamt inkl. MwSt. 99,00 €", "99.00"), ("Total with delivery €99", "99.00"),
    ("Total €99.00\nTotal savings: €120.00\nTotal tax €200.00", "99.00"), ("Total after discount €99.00", "99.00"), ("Total VAT included €99", "99.00"),
    ("Total savings: €12.00\nYou pay: €99.00", None), ("Total tax €20.00", None), ("Total excl. VAT €80", None), ("Total per person €8", None),
    ("Total €50.00\nGrand total 1234.00", None), ("Order total\nIncl. VAT €20.00\n€99.00", None), ("Total €50\nTotal 3 x €40", None),
    ("Total €50\nTotal weight: 2.5 kg\nTotal items: 3", "50.00"), ("Total €50\nTotal weight: 2.5 kg\n3 items in total", "50.00"),
    ("Total 85 incl. VAT €13.71", None), ("Summa 85 sis. alv 16,45 €", None), ("Total: 85 (incl. delivery €4.90)", None),
    ("Total (incl. fees €4.90): $1,234.00", None), ("Total CHF 1'234.00\nTotal tip €2.00", None), ("Total fees €4.00\nYou pay €104.00", None),
    ("Total €100.00 excl. VAT\nAmount payable €124.00", None), ("Total: 12 payments of €99.00", None), ("Total due now €4", None),
    ("Total loyalty balance €4", None), ("Total for 2 nights: €200", "200.00"), ("Order total (3 items)\n€50.00", "50.00"),
    ("Grand total EUR 1 234,50", "1234.50"), ("Total €50.00\nTotal extras €8", "50.00"), ("Total amount payable: 50 €", "50.00"),
])
def test_the_total_that_is_charged(text, total):
    from ampi_tools import checkout as co
    got = grand_total(co.extract_totals(text), "EUR", "EUR")
    assert got == (None if total is None else Decimal(total)), co.extract_totals(text)


def test_kr_counts_as_the_card_currency(tmp_path):
    d, _, ctl = make(tmp_path, currency="SEK")
    ctl.totals = [{"amount": "23.90", "currency": "KR", "label": "Totalt"}]
    res = run(d.run(req("checkout.pay", **{**PAY, "currency": "SEK"}), None, origin="private"))
    assert res.get("approval")


def test_failure_after_filling_is_unknown_and_counted(tmp_path):
    d, side, ctl = make(tmp_path)
    ctl.fail_pay = "the checkout browser failed; the session was closed"  # no "verify" phase: may have gone through
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    out = approve(d, res)
    assert out["status"] == "failed" and "MAY have gone through" in out["text"]
    assert side.vault.purchases()[0]["status"] == "unknown" and side.vault.purchases_today() == 1


def test_definite_refusal_is_not_logged(tmp_path):
    from ampi_tools.ops_checkout import CheckoutRefused
    d, side, ctl = make(tmp_path)

    async def refuse(req, timeout=None):
        if req["cmd"] == "pay":
            raise CheckoutRefused("checkout: the page changed; nothing was filled")
        return await FakeCtl.call(ctl, req, timeout)
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    ctl.call = refuse
    out = approve(d, res)
    assert out["status"] == "failed" and "nothing was filled or paid" in out["text"] and not side.vault.purchases()


def test_stale_pay_request_doesnt_block(tmp_path):
    d, side, ctl = make(tmp_path)
    run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    t0 = side.clock()
    side.clock = lambda: t0 + 700  # the first request's 10 minutes are over
    res = run(d.run(req("checkout.pay", **PAY), None, origin="private"))
    assert res.get("approval")


@pytest.mark.parametrize("label", ["Place order", "Pay now", "Tilaa ja maksa", "Complete purchase"])
def test_plain_click_on_an_order_button_is_refused(tmp_path, label):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"] = label
    out = run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))
    assert not out["ok"] and "places the order" in out["error"]
    assert not [c for c in ctl.calls if c["cmd"] == "click"]


@pytest.mark.parametrize("label", ["Submit", "Request to Book", "Reserve", "Записаться", "Κράτηση", "Cancel my booking"])
def test_plain_click_on_a_committing_button_is_refused(tmp_path, label):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"] = label
    out = run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))
    assert not out["ok"] and "checkout submit" in out["error"] and not [c for c in ctl.calls if c["cmd"] == "click"]


@pytest.mark.parametrize("label,posts,ok", [("Search", True, True), ("Next", True, True), ("Add to cart", True, True),
                                            ("18:30", False, True), ("OK", True, False), ("Go", False, True),
                                            ("Checkout", True, True), ("Secure checkout →", True, True),
                                            ("Add to cart - $12.00", True, True), ("Add 2 items to bag", True, True),
                                            ("Standard shipping €4.90", False, True), ("12:30 (€15)", False, True),
                                            ("Save and continue", True, True), ("Next: payment", True, True),
                                            ("Use this address", True, True), ("Continue to payment", True, True),
                                            ("Check out (2) · 24,00 €", True, True), ("Continue as guest", True, True),
                                            ("Continue to order summary", True, True), ("€15", False, False),
                                            ("Tip $5", False, False), ("P€1ay", False, False), ("Quick checkout", True, False),
                                            ("Instant checkout", False, False), ("Continue with email", True, False),
                                            ("Accept", True, False), ("Accept offer", True, False), ("Accept all", True, True), ("Continue €45", False, False),
                                            ("Next €45", True, False), ("Confirm choices", True, False),
                                            ("Confirm my choices", True, False), ("Go to checkout €4", False, False),
                                            ("Add €12", False, False), ("Рay", False, False), ("Ｐay now", False, False),
                                            ("Dodaj do koszyka 49,99 zł", True, True), ("Review order", True, True),
                                            ("Fri 26 - €15", False, True), ("Choose €45", False, True),
                                            ("Continue to buy", False, False), ("Continue to PayPal", False, False),
                                            ("Express checkout", False, False), ("Pay €80", False, False),
                                            ("Buy now - €12", False, False), ("Checkout with PayPal", False, False),
                                            ("Add to cart and pay", False, False), ("Book for 25 €", False, False),
                                            ("Place order and continue", False, False), ("Next", False, True),
                                            ("Apply filters", True, True), ("Submit search", False, False)])
def test_post_forms_count_as_committing(tmp_path, label, posts, ok):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label=label, posts=posts, id="b")
    assert run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"] is ok


def test_gate_looks_at_every_caption_and_ignores_ids(tmp_path):
    d, _, ctl = make(tmp_path)
    b = ctl.elements["f0:20"]
    click = lambda: run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]  # noqa: E731
    b.update(label="View slots", texts=["View slots", "Pay €80"])
    assert not click()
    b.update(texts=["Pay now"], id="next")
    assert not click()
    b.update(texts=["Submit"], id="search-submit")
    assert not click()
    b.update(texts=["26"], id="booking-day-26")       # a calendar cell
    assert click()
    b.update(texts=["Schedule"], tag="a", dest=SHOP + "/schedule")   # a plain link on the same site
    assert click()
    b.update(dest="https://other.example/x")
    assert not click()
    b.update(tag="button", texts=["OK"], id="x")
    assert click()
    ctl.filled = True                                   # the owner's details are in the page now
    assert not click()
    b.update(texts=[])
    assert not click()


def test_links_that_act_as_buttons_are_gated(tmp_path):
    d, _, ctl = make(tmp_path)
    b = ctl.elements["f0:20"]
    click = lambda: run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]  # noqa: E731
    b.update(tag="a", id="x")
    for text in ("Book", "Send", "Submit", "Reserve", "Register", "Unsubscribe", "Delete", "Cancel my booking"):
        for dest in ("", SHOP + "/page#", SHOP + "/page#book", "javascript:void(0)"):
            b.update(texts=[text], dest=dest)
            assert not click(), (text, dest)
    b.update(texts=["Register"], dest=SHOP + "/register")
    assert click()
    b.update(texts=["Delete my account"])
    assert not click()
    b.update(texts=["Register"])
    ctl.filled = True
    assert not click()


@pytest.mark.parametrize("label", ["Kup teraz", "Zapłać", "Commander avec obligation de paiement", "Bestelling plaatsen",
                                   "Realizar pedido", "Acquista", "Завершить заказ", "הזמן עכשיו", "Get it now", "Rent now",
                                   "Accept offer", "Start free trial", "One-click", "Weiter", "", "OK", "Checkout", "Next: payment"])
def test_once_something_is_entered_and_a_price_shows_unknown_buttons_ask(tmp_path, label):
    d, _, ctl = make(tmp_path)
    b = ctl.elements["f0:20"]
    b.update(label=label, id="x")
    ctl.filled, ctl.gate_totals = True, TOTAL
    assert not run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]
    ctl.gate_totals, ctl.frames = [], [SHOP, STRIPE]
    assert not run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]


@pytest.mark.parametrize("label", ["Next", "Add to cart", "Remove", "+", "18:30", "Standard shipping €4.90"])
def test_harmless_buttons_still_work_there(tmp_path, label):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label=label, id="x")
    ctl.filled, ctl.gate_totals = True, TOTAL
    assert run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"], label


@pytest.mark.parametrize("label", ["Commander", "Bestelling plaatsen", "Завершить заказ", "Hotovo"])
def test_any_price_counts_once_something_is_entered(tmp_path, label):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label=label, id="x")
    ctl.filled, ctl.priced = True, True
    assert not run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]


def test_a_total_in_another_language_makes_submit_refuse(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals = "Reserve", []
    call = ctl.call

    async def with_gate_total(rq, timeout=None):
        r = await call(rq, timeout)
        return {**r, "gate_totals": TOTAL} if rq["cmd"] == "bind_submit" else r
    ctl.call = with_gate_total
    with pytest.raises(ToolError, match="total to pay"):
        run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))


def test_a_priced_booking_page_can_be_submitted_with_a_warning(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals, ctl.priced = "Reserve", [], True
    assert run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))["approval"]
    assert "⚠ The page shows prices: this may place an order" in d.approvals.unnotified()[0]["card"]["details"]


def test_a_form_that_sends_to_another_site_asks(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label="Next", id="x", posts=True, dest="https://other.example/x")
    assert not run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]
    ctl.elements["f0:20"].update(dest=SHOP + "/next")
    assert run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]


TOTAL = [{"amount": "5.00", "currency": "EUR", "label": "Total"}]
SUBMIT = {"ref": "f0:20", "what": "Yoga class Fri 10:00"}


def test_submit_asks_the_owner_and_presses_after_approval(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals = "Book Now", []
    res = run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    assert res["approval"] and not [c for c in ctl.calls if c["cmd"] == "submit"]
    (item,) = d.approvals.unnotified()
    det = item["card"]["details"]
    for x in ("“Book Now” on https://", "Yoga class Fri 10:00", "Email: [your me.email]", "10:00 (selected)",
              "promo: x [not visible on the page]", "Fri 10:00 selected"):
        assert x in det, x
    assert item["card"]["level"] == "high" and "promo" not in res["text"] and "grant_label" not in item
    assert "anna.virtanen" not in str(d.approvals.get(res["approval"]))  # not kept in the request either
    with pytest.raises(ToolError, match="already waiting"):
        run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    out = approve(d, res)
    assert out["status"] == "done" and "Booking confirmed" in out["text"]
    (call,) = [c for c in ctl.calls if c["cmd"] == "submit"]
    assert call["digest"] == "d1" and call["url"].endswith("/book") and call["gen"] == 3


@pytest.mark.parametrize("box,says", [({"box": "checkbox", "checked": False}, "Tick"), ({"box": "checkbox", "checked": True}, "Untick"),
                                      ({"box": "radio", "checked": False}, "Choose")])
def test_a_box_is_ticked_with_approval(tmp_path, box, says):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label="I accept the house rules", tag="input")
    ctl.box, ctl.totals, ctl.gate_totals = box, TOTAL, TOTAL  # (a tick by itself orders nothing, also where there is a total)
    ctl.form = [{"label": "Email", "kind": "input", "value": "•••", "shown": True}]
    res = run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    det = d.approvals.unnotified()[0]["card"]["details"]
    assert det.startswith(f"{says} “I accept the house rules” on") and "Email: (your login)" in det
    assert approve(d, res)["status"] == "done"
    (call,) = [c for c in ctl.calls if c["cmd"] == "submit"]
    assert {k: call[k] for k in ("box", "checked")} == box
    ctl.elements["f0:20"]["tag"] = "div"  # something that only looks like a box
    with pytest.raises(ToolError, match="may be the button that orders"):
        run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    ctl.elements["f0:20"].update(label="I understand that I pay at the desk", tag="input")
    run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    assert "⚠ It says something about paying" in d.approvals.unnotified()[-1]["card"]["details"]


def test_a_field_is_set_with_approval(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"].update(label="Payment", tag="select")
    ctl.box, ctl.totals, ctl.gate_totals = {"set": "select", "option": "Pay at the desk", "was": "Card"}, TOTAL, TOTAL
    res = run(d.run(req("checkout.submit", **SUBMIT, option="Pay at the desk"), None, origin="private"))
    det = d.approvals.unnotified()[0]["card"]["details"]
    assert det.startswith("Set “Payment” to “Pay at the desk” on") and "(now: Card)" in det and "⚠ It says something about paying" in det
    assert approve(d, res)["status"] == "done"
    (call,) = [c for c in ctl.calls if c["cmd"] == "submit"]
    assert call["set"] == "select" and call["option"] == "Pay at the desk"
    assert [c for c in ctl.calls if c["cmd"] == "bind_submit"][0]["option"] == "Pay at the desk"
    with pytest.raises(ToolError, match="something else"):  # (never a press where a value was asked for, or the other way round)
        run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    ctl.box = {}
    with pytest.raises(ToolError, match="something else"):
        run(d.run(req("checkout.submit", **SUBMIT, option="2"), None, origin="private"))


def test_an_arrow_can_be_pressed_with_approval(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals = "›", []
    assert run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))["approval"]


def test_submit_says_when_it_goes_to_another_site(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals, ctl.dest = "Send", [], "https://forms.example/x"
    run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    assert "another site: https://forms.example" in d.approvals.unnotified()[0]["card"]["details"]
    d2, _, ctl2 = make(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    ctl2.elements["f0:20"]["label"], ctl2.totals, ctl2.dest = "Send", [], "http://forms.example/x"
    with pytest.raises(ToolError, match="https"):
        run(d2.run(req("checkout.submit", **SUBMIT), None, origin="private"))


@pytest.mark.parametrize("origin", ["group:x", "forwarded:private", "unknown"])
def test_submit_is_for_the_owners_chat_only(tmp_path, origin):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError):
        run(d.run(req("checkout.submit", **SUBMIT), None, origin=origin))


@pytest.mark.parametrize("label", ["Use this address", "Checkout", "Standard shipping €4.90", "Next: payment", "Apply voucher",
                                   "Save", "Review order", "12:30 (€15)", "Choose €45"])
def test_steps_in_between_can_be_submitted_with_a_total_showing(tmp_path, label):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals, ctl.frames = label, TOTAL, [SHOP, STRIPE]
    assert run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))["approval"]
    assert "⚠ Total on the page: Total 5.00 EUR" in d.approvals.unnotified()[0]["card"]["details"]


@pytest.mark.parametrize("change,err", [
    ({"captcha": True}, "captcha"), ({"risky": ["Subscribe me"]}, "untick"), ({"frames": [SHOP, STRIPE]}, "payment form"),
    ({"frames": [SHOP, "https://www.paypal.com"]}, "payment form"), ({"frames": ["https://shop.app"]}, "payment form"),
    *[({"totals": TOTAL, "label": x}, "total to pay") for x in (
        "Zapłać", "Commander avec obligation de paiement", "Bestelling plaatsen", "Realizar pedido", "Acquista",
        "Завершить заказ", "הזמן עכשיו", "שלם", "Get it now", "Rent now", "Accept offer", "Start free trial", "Weiter")],
    ({"label": "One-click"}, "pay or order"), ({"label": "Рay", "totals": TOTAL}, "alphabets"), ({"label": "€12"}, "price"),
    ({"label": "Add €12"}, "price"), ({"label": "Choose P€1ay"}, "price"), ({"label": "Ｐay now"}, "pay or order"),
    ({"totals": TOTAL}, "total to pay"), ({"totals": TOTAL, "label": "Finish"}, "total to pay"),
    ({"totals": TOTAL, "label": "Confirm"}, "total to pay"), ({"totals": TOTAL, "label": ""}, "total to pay"),
    ({"totals": TOTAL, "label": "Complete my order"}, "total to pay"), ({"frame_origin": STRIPE}, "payment provider"),
    ({"frame_origin": "https://www.paypal.com"}, "payment provider"), ({"label": "Continue to buy"}, "pay or order"),
    ({"label": "Pay now"}, "pay or order"), ({"label": "Book for 25 €"}, "shows a price"), ({"label": "Оплатить"}, "pay or order")])
def test_submit_refusals(tmp_path, change, err):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals = change.get("label", "Reserve"), []
    ctl.elements["f0:20"]["frame_origin"] = change.get("frame_origin", SHOP)
    for k, v in change.items():
        setattr(ctl, k, v)
    with pytest.raises(ToolError, match=err):
        run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))


def test_submit_is_not_for_watchers():
    from ampi_tools import watch_spec as ws
    assert "checkout.submit" in ws.NOT_FOR_WATCHERS


def test_submit_unknown_outcome_is_said(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"], ctl.totals = "Reserve", []
    res = run(d.run(req("checkout.submit", **SUBMIT), None, origin="private"))
    ctl.fail_pay = ToolError("checkout: boom")
    out = approve(d, res)
    assert out["status"] == "failed" and "MAY have gone through" in out["text"]


def test_plain_click_on_continue_is_fine(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:20"]["label"] = "Continue to shipping"
    assert run(d.run(req("checkout.click", ref="f0:20"), None, origin="private"))["ok"]


def test_fill_refuses_selects_widgets_and_http(tmp_path):
    d, _, ctl = make(tmp_path)
    ctl.elements["f0:6"] = {"ref": "f0:6", "frame": "f0", "frame_origin": SHOP, "kind": "select", "tag": "select",
                            "autocomplete": "country", "name": "country", "label": "Country"}
    with pytest.raises(ToolError, match="checkout choose"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f0:6", "field": "address.country"}]), None, origin="private"))
    ctl.elements["f4:1"] = {**ctl.elements["f0:4"], "ref": "f4:1", "frame": "f4", "frame_origin": "https://chat.widget"}
    with pytest.raises(ToolError, match="not the shop's own page"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f4:1", "field": "email"}]), None, origin="private"))
    ctl.origin = "http://shop.example"
    with pytest.raises(ToolError, match="https"):
        run(d.run(req("checkout.fill", fills=[{"ref": "f0:4", "field": "email"}]), None, origin="private"))


def test_a_grant_needs_fields_that_say_what_they_are(tmp_path):
    d, side, ctl = make(tmp_path)
    side.vault.grant(SHOP, ["email"])
    ctl.elements["f0:4"]["autocomplete"] = ""
    ctl.elements["f0:4"]["type"] = "text"  # only its label says "Email": ask instead of filling silently
    res = run(d.run(req("checkout.fill", fills=[{"ref": "f0:4", "field": "email"}]), None, origin="private"))
    assert res.get("approval")


@pytest.mark.parametrize("field,binding", [
    ("address.line1", {"label": "Email address"}), ("name", {"type": "search", "label": "Search by name"}),
    ("phone", {"label": "Hotel"}), ("email", {"label": "Subscribe to our newsletter (email)"}),
    ("address.line1", {"autocomplete": "email", "label": "Address"}),
])
def test_field_matches_refuses_lookalikes(field, binding):
    assert not field_matches(field, binding)


@pytest.mark.parametrize("op,args", [("info.fields", {}), ("payment.log", {}), ("info.revoke", {"site": "all"})])
def test_read_ops_are_private_too(tmp_path, op, args):
    d, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="own chat"):
        run(d.run(req(op, **args), None, origin="watcher:x"))


def test_masking_separators_and_encodings():
    out = mask_text("4242/4242/4242/4242 · anna%40x.fi · Anna+Virtanen · 4000 0566 5566 5556",
                    ["anna@x.fi", "Anna Virtanen"], CARD)
    assert "4242" not in out and "anna%40" not in out and "Anna+" not in out and "5556" not in out


def test_cli_parsers():
    from ampi_tools import client as cli
    a = cli.checkout_parser().parse_args(["pay", "f2:3=card.number", "--button", "f0:20", "--amount", "23.90",
                                          "--currency", "EUR", "--what", "x"])
    assert cli._fill_pairs(a.pairs) == [{"ref": "f2:3", "field": "card.number"}]
    with pytest.raises(SystemExit):
        cli._fill_pairs(["f2:3"])
    _ = json
