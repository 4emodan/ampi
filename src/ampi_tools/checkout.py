"""ampi-checkout: the checkout browser's controller (standard library only; user `ampicheckoutctl`).

It drives the Obscura engine (user `ampicheckout`, `obscura serve` on 127.0.0.1:19333, see
deploy/ampi-checkout-engine.service) over the Chrome DevTools Protocol, or for one session, when it was opened so, the second
engine (Lightpanda, user `ampicheckoutlp`, 127.0.0.1:19334, deploy/ampi-checkout-lp.service): for sites the first can't run.
Card details never go into the second one, and it is thrown away after every session. It answers the ampi-tools daemon on
/run/ampi-checkout.sock (one JSON line in, one JSON line out). Only the daemon may connect (SO_PEERCRED).

What the assistant gets from here is a *snapshot*: the page's text and a numbered list of the things it can
use (inputs, buttons, links, selects, checkboxes), never what is typed into a field. Card and personal values
arrive only in `fill`/`pay` requests, which the daemon sends after the owner approved them, and are set into
exactly the elements that were approved:

- Every frame gets its own isolated JavaScript world (Page.createIsolatedWorld). The script there numbers the
  elements and keeps them in a Map that lives only in that world: page scripts can't see it, renumber it or
  swap an element in it. A ref is (frame, number); an approval binds the element's frame, the frame's origin
  and its tag/type/autocomplete/name/id, and all of it is checked again right before a value is set.
- Values are set through the element's native value setter plus input/change/blur events in that world, never
  by focusing and typing (a page could move the focus).
- Frames with an opaque origin (about:, data:, blob:, srcdoc, javascript:) are never actionable.
- After `pay` the session is read-only: the assistant can look, not click or type (3-D Secure is the owner's).

`submit` presses a button that commits to something (book, send, reserve) after the owner approved the site, the
button and everything filled in. Only for that, `bind_submit` reads the page's field values (never of password,
card, code or hidden fields), for the owner's approval message; any change of them before the press refuses it.

`bind_login` and `fill_login` are only for ampi-signin (another uid, and it may ask for nothing else): they say which site
the sign-in form is really on, and fill in a login the owner unlocked on their phone for exactly that site. The password
goes only into a password field of that site's own page, and is cut out of everything this answers afterwards.
`bind_code` and `fill_code` do the same with the one-time code a site asks for next: only where that sign-in itself led (see
Session.chain), because such codes also confirm payments and changes of an account. `fill_session` opens a new session with the
cookies of one site that the owner gave from their own browser: an account from its first moment.

No arbitrary JavaScript from the assistant, no other reading of input values, no console/network events forwarded,
no downloads, no file:// pages, no stealth and no captcha solving.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import re
import secrets
import select
import socket
import struct
import sys
import time
import unicodedata
import urllib.parse
from collections.abc import Callable
from typing import Any

if __name__ == "__main__":  # deployed next to browser.py in /usr/local/lib/ampi-checkout (-I drops the script dir)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from . import browser as br
except ImportError:  # pragma: no cover - deployed layout
    import browser as br  # type: ignore[no-redef]

CDP_HOST = "127.0.0.1"   # literal: never "localhost", which could resolve to ::1
CDP_PORT = 19333
ENGINES = {"obscura": 19333, "lightpanda": 19334}  # name -> its port on CDP_HOST
SOCKET_PATH = os.environ.get("AMPI_CHECKOUT_SOCKET", "/run/ampi-checkout.sock")
IDLE = 10 * 60
HARD_MAX = 30 * 60
MIN_MEM_MB = 900
NAV_TIMEOUT = 25
PAY_SETTLE = 30
SIGNIN_SETTLE = 12
PAY_FILL_SETTLE = 1.5  # seconds between filling the card and pressing Pay
MAX_REQUEST = 262_144
MAX_TEXT = 60_000
MAX_FRAMES = 20
MAX_ELEMENTS = 400
CALL_TIMEOUT = 30

# Field kinds that only `fill`/`pay` may set (never `type`): card, CVC, passwords, one-time codes, bank accounts.
SECRET_FIELD = re.compile(r"cc-|card|cvc|cvv|csc|password|passcode|passwd|otp|one-time|onetime|\bpin\b|pincode|"
                          r"iban|account|security.?code", re.I)
TEXT_TYPES = {"", "text", "email", "tel", "search", "url", "number"}
# A field for a one-time code. Ampi never types into one (it reads the owner's email, where some of them arrive).
CODE_WORDS = re.compile(
    r"\bone.?time.?(code|pass\w*|pin|token)|\bt?otp|\bmfa|\b2fa|two.?factor|two.?step|\b2.?step|\bpasscode|\b(v|auth|sms|verify) ?code\b|\b(digit|character) ?\d|"
    r"\b(verif\w*|auth\w*|security|sms|log.?in|sign.?in|backup|recovery|(e-?)?mail(ed)?|text(ed)?|confirm\w* e-?mail|e-?mail confirm\w*) (code|token|pin)\b|"
    r"\b(code|pin) (we|you|sent|from|by|in your|received)\b|"
    r"código de (verificaci|seguridad|acceso|verifica)|code de (vérification|sécurité|validation)|codice (di )?(verifica|sicurezza)|"
    r"kod (weryfikacyjny|bezpieczeństwa|z (sms|e-?mail\w*))|verificatiecode|beveiligingscode|código de verificação|code (reçu|envoyé)|"
    r"vahvistuskoodi|kertakäyttö|todennuskoodi|turvakoodi|engångskod|verifieringskod|säkerhetskod|bestätigungscode|"
    r"sicherheitscode|einmal.?code|код подтверждения|код из (sms|смс|письма)|одноразов|κωδικός (επαλήθευσης|επιβεβαίωσης)|μιας χρήσης", re.I)
NOT_CODE = re.compile(
    r"promo|coupon|voucher|\bgift|discount|referral|\binvit|postal|\bpost|\bzip|\barea code|\bcountry|\bdial|\bproduct|\bcourse|\bclass(es)?\b|\blesson|\bevent|\bbooking|"
    r"\breservation|\border|\btracking|\bgroup (code|no|number|id)|\bsku\b|barcode|\biban|\bbic\b|\bswift|\btax|\bvat\b|\bqr\b|\binitial|alennus|kampanja|lahja|rabatt|gutschein|"
    r"промокод|купон|индекс|έκπτωση|ταχυδρομικ", re.I)
CODE_BARE = re.compile(r"(please )?(enter |type )?(the |your )?(\d.?digit )?(code|token|pin|pin code|koodi|kod|kode|código|codice|код|κωδικός)( here)?( ?\d{1,2})?", re.I)
# Also asked for to find a booking, so not refused when Ampi types; enough where a sign-in asks for a code.
CODE_LOOSE = re.compile(r"\b(confirm\w*|access|digit|character) ?(code|\d)|\b(code|koodi|kod|kode|código|codice|код|κωδικό\w*)\b|\b\w{2,8}code\b|\b\d.?digits?\b|\bdigits\b", re.I)
CODE_TYPES = {"", "text", "tel", "number", "password"}
# What a page says where it wants one; there Ampi types only into fields that plainly are for something else.
CODE_SENT = re.compile(
    r"\b(sent|e-?mailed|texted|mailed)\b.{0,80}\b(code|pin)\b|\b(code|pin)\b.{0,60}\b(sent|e-?mailed|texted)\b|check your (e-?mail|inbox|phone|messages) for.{0,30}\b(code|pin)\b|"
    r"\b(enter|type|use)\b.{0,40}\b(code|pin)\b.{0,80}\b(e-?mail|phone|sms|text|app|authenticator)|(verification|confirmation|one.?time|sign.?in|login) (code|password)|"
    r"two.?(factor|step)|\d chiffres|code (reçu|envoyé)|(wysłaliśmy|wysłano|podaj|wpisz).{0,40}\bkod|\bkod z\b|vahvistuskoodi|kertakäyttö|engångskod|bestätigungscode|"
    r"código de (verif|seguridad|acceso|confirm)|code de (vérification|sécurité)|codice di (verif|sicur)|"
    r"код подтверждения|отправили.{0,40}код|κωδικό.{0,20}(επαλήθευσης|που σας)", re.I)
OTHER_CODE = re.compile(r"\b(promo\w*|discount|coupon|voucher|gift( card)?|referral|postal|post|zip|area|door|dress|country|booking|reservation) codes?\b", re.I)
PLAIN_FIELD = re.compile(
    r"(your |the )?((first|last|full|given|family|user|company|card ?holder) ?)?name|e-?mail( address)?|(phone|mobile)( number)?|search|q|query|(street )?address( line)?( ?\d)?|"
    r"city|town|zip|post(al)? ?code|zip ?code|country|company|organi[sz]ation|subject|message|comments?|notes?|birth ?date|date( of birth)?|nickname|title|quantity|qty|"
    r"nimi|etunimi|sukunimi|sähköposti|puhelin(numero)?|osoite|postinumero|kaupunki|viesti", re.I)
CODE_MAX = 16
CODE_WINDOW, CODE_STEPS, CODE_TRIES = 600, 5, 3
# What the buttons and links on the way to a code say, whole (login_caption).
CODE_STEP = re.compile(
    r"resend|(text|e-?mail|call) me( (a|the|my)( new)? code)?|((re)?send|get|request)( me)?( (a|an|the|my|another))?( new)?"
    r"( (verification|security|login|one.?time|sms))? ?(code|sms|text|e-?mail|text message|call)( again| via sms| by (sms|text|e-?mail|phone)| to [^\s]*[•*x.\d]{2,}[^\s]*)?|"
    r"(try|use|choose|show)( (a|an))?( different|another|other)( (way|method|option|verification method))( to (sign|log) in)?|"
    r"(use|try|enter)( (a|an|my|your|the))? ((authenticator|authentication)( app)?|backup code|recovery code|sms|text message|e-?mail|verification code|security code|code)( instead)?|"
    r"didn.?t (get|receive) (a|the|your) code|lähetä( uusi)? koodi|skicka( ny)? kod|code senden|отправить код|αποστολή κωδικού", re.I)
# A way to get the code, as a button; often with the beginning of where it goes.
CODE_METHOD = re.compile(r"((by|via|with|use) )?(text message|sms|text|e-?mail|authenticat(or|ion)( app)?|phone call|(phone|voice) call|call|backup codes?|recovery codes?|whatsapp|tekstiviesti|sähköposti)"
                         r"( (code|me|to|at|app))*( [^\s]*[•*x.]{2,}[^\s]*)?", re.I)
CODE_GENERIC = re.compile(r"continue|next|jatka|seuraava|fortsätt|nästa|weiter|далее|продолжить|συνέχεια|continuer|continuar|continua|verder|dalej", re.I)
CODE_AGAIN = re.compile(r"((re)?send|get|request)( me)?( (a|the|my|another))?( new)?( (verification|security|login|one.?time|sms))? ?code( again)?|resend|lähetä( uusi)? koodi|"
                        r"skicka( ny)? kod|code (erneut )?senden|отправить код( ещё раз)?", re.I)
STEP_AWAY = re.compile(r"\b(settings?|preferences|profile|account|wallet|billing|security|e-?mails?|phones?|devices?|members?|send)\b", re.I)
PLACE_QUERY = re.compile(r"err(or)?\w*|invalid\w*|fail\w*|retry|attempts?|msg|message|status|result|resen[dt]|sent|lang|locale", re.I)
STEP_QUERY = frozenset({"method", "channel", "via", "type", "factor", "provider", "lang", "locale", "resend", "mode", "step"})
CODE_WAY = re.compile(r"\b(sms|text|e-?mail|phone|call|whatsapp|authenticator|app|code|koodi|kod|tekstiviesti|sähköposti|смс|почт\w*|κωδικό\w*)\b|•|\*\*", re.I)
CODE_CAPTION = re.compile(
    r"(verify|confirm|submit|check|send|enter)( (the )?code)?|(verify|confirm)( and| &)? ?(continue|sign ?in|log ?in)|done|ok|vahvista( koodi)?|lähetä|"
    r"bekräfta|verifiera|bestätigen|prüfen|bekreft|bekræft|bevestigen|vérifier|valider|verificar|confirmar|verifica|conferma|"
    r"potwierdź|zweryfikuj|подтвердить|проверить|отправить|επιβεβαίωση|επαλήθευση|υποβολή", re.I)
# A page where a code would confirm something else than a sign-in.
CODE_DANGER = re.compile(
    r"\bpay(ing|ments?|able|ee)?\b|purchase|transfer|withdraw|refund|\biban\b|beneficiary|recipient|\b(confirm|place|pay for|complete|approve|authori[sz]e) (your|the|this|an?) order\b|"
    r"(credit|debit|bank|payment) card|3-?d ?secure|verified by visa|securecode|\bid check\b|subscription|"
    r"\bnew (e-?mail|(phone|mobile)( ?number)\b|number|address|password|passkey|authenticator)|"
    r"\b(update|replace|remove|add|reset)( (a|an|your|my|the|new))? (e-?mail|phone|mobile|number|address|password|passkey|authenticator|device key)|"
    r"\bchange( (a|an|your|my|the))?( new)? (e-?mail|phone|mobile|number|address|passkey|authenticator)|"
    r"delete|deactivat|disable|turn off|close (your|my|the) account|"
    r"\bmaksu(?!to)\w*|\bmaksa\w*|tilisiirto|poista|uusi (puhelin\w*|sähköposti\w*|salasana\w*)|vaihda|betal\w*|överför\w*|ta bort|nytt lösenord|"
    r"bezahl\w*|zahlung\w*|überweis\w*|lösch\w*|neue[sn]? (passwort|telefon\w*|nummer|e-?mail)|"
    r"оплат\w*|плат[её]ж\w*|перевод\w*|удали\w*|удале\w*|удаля\w*|нов\w+ (парол\w*|телефон\w*|номер\w*|почт\w*)|смен[аи]\w*|"
    r"πληρωμ\w*|μεταφορ\w*|διαγραφ\w*|νέο\w* (κωδικό\w* πρόσβασης|τηλέφωνο|email)", re.I)
CODE_HEAD = re.compile(r"change( (your|my|the))? password|password change|\bcheckout\b|\b(your|the|this) order\b", re.I)  # (elsewhere on a page it is advice, or a list)
# An address that leads further than to a sign-in (in a sign-in page's own address, a sign-in for another app is ordinary).
CODE_LEADS = re.compile(CODE_DANGER.pattern + r"|\b(settings?|preferences|security|2fa|mfa|two.?factor|password|confirm\w*|approv\w*|edit|wallet|profile|devices?|phone|e-?mail|checkout)\b", re.I)
CODE_DEEP = re.compile(CODE_LEADS.pattern + r"|\bauthori[sz]e\b", re.I)
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}")
COOKIE_VALUE, COOKIE_PATH = re.compile(r"[\x20-\x3a\x3c-\x7e]{0,4096}"), re.compile(r"/[\x21-\x3a\x3c-\x7e]{0,500}")
# They vouch for "a person in that very browser": taking them along would get around a site's bot check. (The extension leaves them
# out, too.)
CLEARANCE = re.compile(
    r"(cf_clearance|__cf_bm|__cflb|__cfruid|_cfuvid|cf_chl_.*|_abck|ak_bmsc|bm_(sz|sv|mi|so|s|ss|lso)|sbsd(_.*)?|sec_cpt|"
    r"datadome|_px(\d|hd|m?vid|de|cts|captcha|ff.*|_.*)?|pxcts|reese84|___utmv.*|incap_ses_.*|visid_incap_.*|nlbi_.*|aws-waf-token|kp_uid.*|"
    r"x-kpsdk-.*|__ddg.*|_grecaptcha|rc::.*|frcaptcha.*|h-captcha.*|_vcrcs|__uzm.*|uzm.*|rbzid.*|rbzsessionid|sucuri_.*|"
    r".*anubis-auth.*|acw_sc__.*|acw_tc|_fs_ch_.*|qrator_.*|d_[a-z]id|arkose.*|_iidt|geetest.*)", re.I)
CODE_FRAME = re.compile(r"3-?ds|threeds|three-d|/acs\b|acs\.", re.I)
CODE_SIGNUP = re.compile(r"\b(regist(er|ration)|sign ?up|creat(e|ing) (an? |your )?(new )?account|new account|join (us|now)|reset|forgot(ten)?|new pin|set (a|your) pin|"
                         r"rekisteröidy|luo tili|registrieren|konto erstellen|skapa konto|регистрация|εγγραφή)\b", re.I)
# Where a session may begin for a code to go in later: a site's start page, or an address that is plainly its sign-in page.
ENTRY_LOGIN = re.compile(r"([a-z]+[-_])?(log|sign)[-_]?(in|on)([-_][a-z]+)?|auth|authenticate|authentication|sso|kirjaudu(-sisaan)?|logga-in|"
                         r"anmelden|anmeldung|connexion|acceso|entrar|accedi|inloggen|zaloguj|vhod", re.I)
ENTRY_HOME = re.compile(r"sessions?(/new)?|(my[-_]?)?account|oma-?tili|mon-compte", re.I)  # only as they are
ENTRY_LAST = re.compile(r"e-?mail|password|new|start|identifier|username|user|form|index", re.I)
ENTRY_LANG = re.compile(r"(en|de|fr|es|it|pt|nl|sv|fi|nb|nn|da|pl|cs|sk|hu|ro|bg|el|et|lv|lt|sl|hr|ru|uk|tr|ja|ko|zh|he|ar|ca|eu|ga|mt)([-_][a-z]{2})?", re.I)
ENTRY_QUERY = re.compile(r"lang|locale|language|hl|utm_\w+", re.I)
ENTRY_NEXT = frozenset("next redirect redirect_to redirecturl redirect_uri return returnto return_to returnurl return_url callbackurl callback continue dest destination route page from r".split())
# Of the sign-in for another app: says nothing about where it leads.
LEAD_SKIP = frozenset("response_type response_mode prompt claims acr_values ui_locales audience resource code_challenge code_challenge_method state nonce".split())
LOGIN_NAV = re.compile(
    r"(log|sign) ?(in|on)( here| now)?|(log|sign) ?in ?(/|or|&|and) ?(sign ?up|register|join|create( an)? account)|open (the )?(log|sign) ?in( dialog| form| menu| window)?|"
    r"toggle (navigation|menu)|(main )?navigation|(my )?account|my page|members?( area| log ?in)?|(client|customer|member) (log|sign) ?in|"
    r"((log|sign) ?in|continue) with (your )?(e-?mail|password)( address)?|(use|with)( your| my)? (e-?mail|password)( instead)?|e-?mail|password|menu|open menu|already a member\??( (log|sign) ?in)?|"
    r"i already have an account|kirjaudu( sisään)?|oma tili|logga in|mina sidor|anmelden|einloggen|mein konto|inloggen|connexion|se connecter|mon compte|"
    r"iniciar sesión|mi cuenta|accedi|zaloguj( się)?|войти|вход|личный кабинет|σύνδεση|είσοδος|ο λογαριασμός μου", re.I)
NAV_NOT = re.compile(r"\b(order|book(ing)?|confirm|cancel|accept|approve|agree|reserve|subscribe|buy|checkout|submit|send|pay)\b", re.I)
LOGOUT = re.compile(r"(log|sign) ?(out|off)|kirjaudu ulos|logga ut|logg ut|log ud|ausloggen|uitloggen|(se )?déconnecter|déconnexion|cerrar sesión|"
                    r"disconnetti|wyloguj( się)?|выйти|αποσύνδεση|logi välja", re.I)
COOKIE_CAPTION = re.compile(r"(decline|reject|deny|accept|allow)( all)?( cookies)?|got it|ok(ay)?|i (agree|accept|understand)|agree|understood|(only )?necessary( cookies)?( only)?|close|×|dismiss", re.I)
RISKY = re.compile(r"subscri|recurring|membership|auto-?renew|save (my )?card|remember (this |my )?card|\blink\b|"
                   r"tilaus|jäsenyys|kestotilaus", re.I)
TOTAL_WORDS = re.compile(r"\b(grand total|order total|total|subtotal|to pay|amount due|summa|yhteensä|maksettava|"
                         r"totalt|att betala|gesamt|summe)\b", re.I)
# More words, only for deciding whether a button needs the owner. Never for `pay`: some of them also begin a subtotal line,
# and pay must refuse rather than take that for the amount charged.
GATE_WORDS = re.compile(r"\b(σύνολο|πληρωτέο|итого|к оплате|razem|do zapłaty|suma|totale|à payer|te betalen|totaal|kokku|"
                        r"a pagar|zu zahlen)\b", re.I)
QUANTITY = re.compile(r"\b(qty|quantity|amount of (people|guests|items)|guests?|people|persons?|adults?|children|kids|seats?|"
                      r"tickets?|nights?|pcs|pieces|count|määrä|anzahl|кол-?во|количество|ποσότητα)\b", re.I)
PRICE = re.compile(r"(?:€|\$|£|₽|₪|zł)\s?\d|\d\s?(?:€|\$|£|₽|₪|zł|(?:eur|usd|gbp|sek|nok|dkk|pln|chf|kr|руб)\b)", re.I)
CURRENCY_CODES = {"EUR", "USD", "GBP", "SEK", "NOK", "DKK", "PLN", "CHF", "CZK", "HUF", "ISK", "CAD", "AUD", "NZD",
                  "JPY"}
# "kr" stays "KR" (Swedish, Norwegian, Danish, Icelandic krona all print as kr): the daemon maps it to the card's.
SYMBOLS = {"€": "EUR", "$": "USD", "£": "GBP", "kr": "KR", "zł": "PLN", "CA$": "CAD", "C$": "CAD", "A$": "AUD",
           "AU$": "AUD", "US$": "USD", "NZ$": "NZD"}
_CUR = r"(?:CA|AU|US|NZ|A|C)\$|€|(?<![^\W\d_])\$|£"
CONFIRMATION = re.compile(r"order|confirm|thank|kiitos|tilaus|vahvist", re.I)
REF_RE = re.compile(r"f\d{1,3}:\d{1,6}")
# The same as HOST_RE in ampi_phone/signin.py: the sites a login can be saved for.
LOGIN_HOST = re.compile(r"(?=.{4,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])")
USER_FIELD = re.compile(r"user|login|e-?mail|phone|mobile|account|member|customer|\bid\b|käyttäjä|tunnus|sähköposti|benutzer|"
                        r"utilisateur|usuario|utente|użytkownik|användar|bruker|bruger|gebruiker|kasutaja|"
                        r"пользовател|логин|почт|телефон|χρήστη|ηλεκτρονικ", re.I)
MIN_HIDE = 4  # a shorter one would cut pieces out of ordinary words
# What a sign-in button says, whole (login_caption): nothing that could also order, pay, confirm or delete something.
LOGIN_CAPTION = re.compile(
    r"((log|sign) ?(in|on)( (now|securely))?|(log ?in|sign ?in|continue|next) with (your )?(e-?mail|password)|continue|next|"
    r"kirjaudu( sisään)?|jatka|seuraava|logga in|fortsätt|nästa|logg inn|log ind|fortsæt|anmelden|einloggen|weiter|inloggen|aanmelden|"
    r"verder|volgende|(se )?connecter|connexion|continuer|suivant|iniciar sesión|entrar|acceder|continuar|siguiente|accedi|continua|"
    r"avanti|zaloguj( się)?|dalej|войти|вход|далее|продолжить|σύνδεση|είσοδος|συνέχεια|επόμενο|εισοδος|συνδεση|logi sisse|edasi)", re.I)
PAY_HOST = re.compile(r"(^|[./])(stripe|adyen|braintreegateway|paypal|paypalobjects|klarna|klarnaservices|shopifycs|squareup|squarecdn|"
                      r"paytrail|mollie|worldpay|jccsmart|vivapayments|revolut)\.(com|net|app|io|fi|eu)$")
# The parts of a reply that are the page's words (the rest are this controller's own, and must not be touched).
PAGE_WORDS = frozenset({"title", "text", "label", "texts", "around", "options", "selected", "risky_checked", "checked", "url", "href",
                        "dest", "name", "id", "ident", "value", "confirmation", "error"})
_SP = " \u00a0\u2007\u2009\u202f"  # spaces that group digits ("1 234,00")
_CODE = r"(?<![^\W\d_])(?i:EUR|USD|GBP|SEK|NOK|DKK|PLN|CHF|CZK|HUF|ISK|CAD|AUD|NZD|JPY|kr|zł|euros?)(?![^\W\d_])"
# A whole run of digits and whatever may separate them (norm_amount says what it is, or that it can't be read): a piece of a
# number must never be taken for the amount. (A tab is the next cell of a table.)
RUN = re.compile(r"\d(?:\d|(?:[^\S\t\n]|[.,'’‘´`·˙，．٫٬:])+(?=\d))*")
PRE_CUR = re.compile(rf"({_CUR}|{_CODE})\)?\s*:?\s*$")
POST_CUR = re.compile(rf"(?:[.,][-–—]{{1,2}})?\s*\(?({_CUR}|{_CODE})")
NOT_MONEY = re.compile(r"\s*(?:%|(?:kg|gr?|km|c?m|mm|min|hrs?|h|hours?|days?|nights?|items?|pcs|pieces?|points?|pts|products?|articles?|"
                       r"persons?|people|guests?|adults?|tickets?|seats?|reviews?|tuotetta|tuotteita|kpl|yötä|henkilöä|artikel|st|stk|"
                       r"товар\w*|шт)\b)", re.I)
# Around an amount: a part of the price, a price per something, a price without something, or a piece of a longer number.
PART = re.compile(r"\s*(?:/|per\b|each\b|pro\b|p\.?p\b|a month|monthly|x\b|×|\*|\(?\s*(?:excl|ex\.? ?(?:vat|tax)|before|without|plus\b|\+))", re.I)
PARTS = re.compile(r"(?:(?<![^\W\d_])[x×*]|\b(?:payments?|instal\w*|rates?|months?|raten?|erää)(?: of)?)\s*$", re.I)
COUNTED = re.compile(r"\b(items?|products?|articles?|quantity|qty|people|persons?|guests?|nights?|tickets?|pcs|tuotteita|määrä|artikel) ?: ?$", re.I)
DATE_OR_TIME = re.compile(r"\d{1,2}\.(?:0?\d|1[0-2])\.(?:20)?\d\d|\d{1,2}:\d\d")
CAPTCHA_FRAME = re.compile(r"hcaptcha\.com/captcha/.*frame=challenge|recaptcha/(api2|enterprise)/bframe|"
                           r"challenges\.cloudflare\.com|captcha-delivery\.com|arkoselabs\.com|funcaptcha", re.I)
THREEDS_FRAME = re.compile(r"3ds|threeds|three-d|/acs\b|acs\.|/challenge|authenticat", re.I)
THREEDS_TEXT = re.compile(r"3-?d ?secure|verified by visa|mastercard (id check|securecode)|verify (your )?(payment|"
                          r"purchase|identity)|authenticat(e|ion) (your )?(payment|purchase)|vahvista (maksu|osto)",
                          re.I)


class Refused(Exception):
    """A request we won't run; the message (never containing request values) goes back to the daemon."""


class CdpError(Exception):
    """The connection to the engine broke; the session is closed."""


class CdpProtocolError(Exception):
    """The engine answered a command with an error (e.g. "Cannot find context" while a page navigates). Routine
    during redirects and frame remounts: the session stays; the caller retries or asks for a new snapshot."""


PAGE_CHANGED = "the page changed; take a new snapshot"


# --- small helpers ------------------------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def has_card_number(text: str) -> bool:
    """A Luhn-valid run of 13-19 digits, allowing spaces, dots and dashes between them."""
    for m in re.finditer(r"\d(?:[ .\- ]?\d){12,22}", text):
        digits = re.sub(r"\D", "", m.group())
        for n in range(13, 20):
            for i in range(0, len(digits) - n + 1):
                if luhn_ok(digits[i:i + n]):
                    return True
    return False


def norm_amount(num: str) -> str | None:
    """"1 234,5" -> "1234.50"; None when it can't be read for sure. ("1.234" and "1,234" are thousands.)"""
    s = re.sub(f"[{_SP}]", " ", num.strip())
    m = (re.fullmatch(r"(\d+)(?:[.,](\d{1,2}))?", s)
         or re.fullmatch(r"([1-9]\d{0,2}(?P<sep>[ .,'’])\d{3}(?:(?P=sep)\d{3})*)(?:(?!(?P=sep))[.,](\d{1,2}))?", s))
    if not m:
        return None
    whole, frac = re.sub(r"\D", "", m.group(1)), m.group(m.re.groups)
    return f"{int(whole)}.{(frac or '0').ljust(2, '0')}"


def read_amounts(line: str, start: int = 0) -> list[tuple[int, str, str]]:
    """(where it begins, the amount or "unreadable (…)", its currency or "?") for what may be money in a line."""
    out = []
    for k, m in enumerate(RUN.finditer(line, start)):
        if k >= 100:  # no honest line
            return [*out, (m.start(), "unreadable (many numbers)", "?")]
        num, off = m.group(), max(0, m.start() - 40)
        before, after = line[off:m.start()], line[m.end():m.end() + 40]
        dot = before[-1:] in (".", ",", "'", "’", "·") and not (before[-2:-1].isalpha() or before[-2:-1] == ".")  # ".50"
        pre, post = PRE_CUR.search(before[:-1] if dot else before), POST_CUR.match(after)
        head, tail = before[:pre.start()] if pre else before, after[post.end():] if post else after
        glued = (not pre and before[-1:].isalpha()) or (not post and after[:1].isalpha())
        part = PARTS.search(head) or PART.match(tail) or re.search(r"\d\W{1,2}$", before)
        names = [c.group(1) for c in (pre, post) if c]
        if names:
            cur = {SYMBOLS.get(c) or SYMBOLS.get(c.lower()) or ("EUR" if c.lower().startswith("eur") else c.upper()) for c in names}
            amount = None if dot or glued or part or len(cur) > 1 else norm_amount(num)
            out.append((off + len(head), amount or f"unreadable ({num[:20]})", sorted(cur)[0]))
        elif dot or not (NOT_MONEY.match(after) or DATE_OR_TIME.fullmatch(num) or glued and num.isdigit()
                         or num.isdigit() and len(num) < 4 and not after.strip() and COUNTED.search(before)
                         or num.isdigit() and len(num) < 4 and re.search(r"\(\s*$", before) and re.match(r"\s*\)", after)):  # "(2)"
            out.append((off + len(head), f"unreadable ({num[:20]})", "?"))
    return out


def extract_totals(text: str, words: "re.Pattern | None" = None) -> list[dict]:
    """Amounts on lines that say total/summa/…, with their currency, for the approval's 'page says' and for `pay`. Where the
    line has none, the next one counts, if it is only an amount. What can't be read for sure says so."""
    out: list[dict] = []
    lines = text.splitlines()[:3000]
    for i, line in enumerate(lines):
        word = (words or TOTAL_WORDS).search(line)
        if not word:
            continue
        found = [(line[word.start():min(at, word.start() + 200)], amount, cur) for at, amount, cur in read_amounts(line, word.end())]
        if found and re.fullmatch(r"[\d.,'’\s]*\d[\d.,'’\s]*", next((x for x in lines[i + 1:i + 3] if x.strip()), "")):
            found = [(label, f"unreadable ({amount})", cur) for label, amount, cur in found]  # the rest of the number, set apart
        if not found:
            after = next((x for x in lines[i + 1:i + 4] if x.strip()), "")
            for at, amount, cur in read_amounts(after):
                alone = not re.search(r"[^\W\d_]", after[:at])
                if alone or cur != "?":
                    found.append((f"{line[word.start():word.start() + 200]} {after[:min(at, 200)]}", amount if alone or amount.startswith("un") else f"unreadable ({amount})", cur))
        for label, amount, cur in found:
            item = {"amount": amount, "currency": cur, "label": re.sub(r"\s+", " ", label).strip(" :(-–—.").lower()[:80]}
            if item not in out:
                out.append(item)
        if len(out) >= 50:
            break
    return out


def totals_key(totals: list[dict]) -> set[tuple[str, str]]:
    return {(str(t.get("amount")), str(t.get("currency"))) for t in totals or [] if isinstance(t, dict)}


def login_host(url: str) -> str:
    """The site a page is on, as logins are saved for it; "" when none can be (not https, a port, an address)."""
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower().removeprefix("www.")
        ok = u.scheme == "https" and u.port in (None, 443) and LOGIN_HOST.fullmatch(host)
    except ValueError:
        return ""
    return host if ok else ""


def _spaced(d: dict, *keys: str) -> str:
    """Names as words: "verificationCode" and "verification_code" are both "verification code"."""
    text = " ".join(str(d.get(k) or "") for k in keys)
    return re.sub(r"[_\-.\[\]:]+", " ", re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text))


def code_field(d: dict, loose: bool = False) -> bool:
    """Is that a field for a one-time code? `loose`: where a sign-in is known to ask for one."""
    if "one-time-code" in str(d.get("autocomplete") or "").lower():
        return True
    each = [_spaced(d, k).strip(" *:") for k in ("name", "id", "label")]
    if any(CODE_WORDS.search(x) for x in each):
        return True
    if any(NOT_CODE.search(x) for x in each):
        return False
    return (any(CODE_BARE.fullmatch(x) for x in each) or loose and any(CODE_LOOSE.search(x) for x in each)
            or d.get("max") == 1 and d.get("type") in CODE_TYPES and not any(QUANTITY.search(x) for x in each))


def a_code(v: Any) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(rf"[A-Za-z0-9](?:[A-Za-z0-9-]{{2,{CODE_MAX - 2}}})[A-Za-z0-9]", v))


def _as_words(address: str) -> str:
    """(A part of) an address as words, for CODE_DANGER."""
    for _ in range(3):
        address = urllib.parse.unquote_plus(address)
    return re.sub(r"[^\w.]+|_+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", address))


def address_words(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return _as_words(f"{u.path} {u.query} {u.fragment}")


def nested(url: str) -> list[str]:
    """The addresses inside an address (`?next=/security/…`): where signing in there leads."""
    u = urllib.parse.urlsplit(url)
    inside = u.fragment.partition("?")[2] or ("" if u.fragment.startswith(("/", "!")) else u.fragment)  # ("#/login" is a page)
    out = []
    for _, text in urllib.parse.parse_qsl(f"{u.query}&{inside}", keep_blank_values=True):
        for _ in range(3):
            text = urllib.parse.unquote(text)
        if "/" in text or "\\" in text or re.search(r"https?:", text, re.I):
            out.append(text)
    return out


def _entry_path(path: str) -> tuple[bool, str]:
    """(is it a start or sign-in page, the path without a last part that says which kind of sign-in)"""
    parts = [re.sub(r"\.(php|html?|aspx?|jsp|do)$", "", x, flags=re.I) for x in path.split("/")]
    parts = [x for x in parts if x and x != "!"]
    if parts and ENTRY_LANG.fullmatch(parts[0]):  # /en/…
        parts = parts[1:]
    if not parts or parts in (["index"], ["home"], ["default"], ["start"]):
        return True, ""
    if ENTRY_HOME.fullmatch("/".join(parts)):
        return True, ""
    if len(parts) > 1 and ENTRY_LAST.fullmatch(parts[-1]) and ENTRY_LOGIN.fullmatch(parts[-2]):  # /login/email
        parts = parts[:-1]
    return bool(ENTRY_LOGIN.fullmatch(parts[-1])) and len(parts) <= 3 and all(re.fullmatch(r"[a-z][a-z_-]{0,24}", x, re.I) for x in parts[:-1]), "/".join(parts)


def _leads_home(address: str, url: str) -> bool:
    """An address inside an address that is only the site's own start or sign-in page."""
    u = urllib.parse.urlsplit(address)
    return (not u.netloc and not u.scheme or u.scheme == "https" and same_site(address, url)) and not u.query and not u.fragment \
        and not address.startswith("//") and "\\" not in address and _entry_path(u.path)[0] and not CODE_DEEP.search(_as_words(_entry_path(u.path)[1]))


def plain_entry(url: str, asked: bool = False) -> bool:
    """A site's start page, or plainly its sign-in page: an address that says nothing about where to go after it. `asked`: in a link
    of the site's own that says "Log in", something may be asked for in it."""
    u = urllib.parse.urlsplit(url)
    route = u.fragment if u.fragment.startswith(("/", "!")) else ""
    (ok, path), (ok_route, words) = _entry_path(u.path), _entry_path(route.partition("?")[0])
    if u.scheme != "https" or not ok or not ok_route or CODE_DEEP.search(_as_words(f"{path} {words}")):
        return False
    if asked:
        return not any(CODE_LEADS.search(x) for x in lead_words(url))
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    return (u.fragment == route and "?" not in route and not any(
        not (ENTRY_QUERY.fullmatch(k) and re.fullmatch(r"[\w.-]{0,30}", v)) and not (k.lower() in ENTRY_NEXT and _leads_home(v, url)) for k, v in pairs))


def lead_words(url: str, depth: int = 0) -> list[str]:
    """What an address says about where it leads: the values in its query (and of addresses in there), as words."""
    u = urllib.parse.urlsplit(url)
    inside = u.fragment.partition("?")[2] or ("" if u.fragment.startswith(("/", "!")) else u.fragment)
    out = []
    for k, v in urllib.parse.parse_qsl(f"{u.query}&{inside}", keep_blank_values=True):
        for _ in range(3):
            v = urllib.parse.unquote(v)
        if k.lower() == "scope" or k.lower() in LEAD_SKIP and "/" not in v:
            continue
        if "/" in v and depth < 3:
            out += [_as_words(urllib.parse.urlsplit(v).path), *lead_words(v, depth + 1)]
        else:
            out.append(_as_words(v))
    return out


def same_site(a: str, b: str) -> bool:
    """The same host, or one above or below the other."""
    x, y = login_host(a), login_host(b)
    return bool(x and y) and (x == y or x.endswith("." + y) or y.endswith("." + x))


_NUMBER = r"\d(?:[\d.,'’\s]*\d)?"
_PRICED = re.compile(rf"(?:€|\$|£|₽|₪|zł)\s?({_NUMBER})|({_NUMBER})\s?(?:€|\$|£|₽|₪|zł|(?:eur|usd|gbp|sek|nok|dkk|pln|chf|kr|руб)\b)", re.I)


def priced(words: str) -> bool:
    """A price, other than nothing ("Cart 0,00 €")."""
    return any(re.search(r"[1-9]", m.group(1) or m.group(2)) for m in _PRICED.finditer(words))


def login_host_of(cookie: dict, host: str) -> str:
    """The name a cookie (as _cookie makes it) is for."""
    return str(cookie.get("domain") or host).lstrip(".")


def fillable(v: Any) -> bool:
    """A user name or password that can be filled in (and cut out again): ordinary characters."""
    return isinstance(v, str) and 0 < len(v) <= 200 and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in v)


def login_caption(text: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip(" →›»>:.!")


class Name(str):
    """The user name of a login. A password is cut out wherever it is; this only where it is a word of its own, and not
    when it could be any number ("info", "2400")."""


class Code(Name):
    """A one-time code: wherever its characters are, also set apart ("482 913"), but not inside a longer number."""


def hider(secrets_: list[str], cut_off: bool = False) -> "re.Pattern | None":
    """Finds the login that was filled in: as it is, or as a URL (once or twice), HTML or JSON writes it. `cut_off`: at the
    end also the beginning of one (of a text that was cut to a length)."""
    parts: list[tuple[int, str]] = []
    for s in secrets_:
        name = isinstance(s, Name)
        if isinstance(s, Code):
            apart = r"[\s\-–.·•\u200b]*".join(re.escape(ch) for ch in s.replace("-", ""))
            parts.append((len(s), rf"(?<!\d){apart}(?!\d)" if s.replace("-", "").isdigit() else apart))
            continue
        if name and s.isdigit() and len(s) < 6:
            continue
        forms: set[str] = set()
        plain = {str(s), unicodedata.normalize("NFC", s), unicodedata.normalize("NFD", s), " ".join(s.split())}
        for v in plain:
            once = {urllib.parse.quote(v, safe=""), urllib.parse.quote(v), urllib.parse.quote_plus(v)}
            forms |= {v, *once, *(urllib.parse.quote(o, safe="") for o in once), html.escape(v), html.escape(v, quote=False),
                      json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]}
        # (Behind %3D the "D" isn't a letter of a word.)
        before = r"(?:(?<!\w)|(?<=%[0-9a-f]{2})|(?<=%25[0-9a-f]{2}))(?<!\d[.,])" if name else ""
        after = r"(?!\w|[.,]\d)" if name else ""
        for f in forms:
            if len(f) >= MIN_HIDE:
                parts.append((len(f), before + re.escape(f) + after))
                if cut_off and f in plain:
                    parts += [(k, before + re.escape(f[:k]) + r"\Z") for k in range(MIN_HIDE, len(f))]
    return re.compile("|".join(p for _, p in sorted(set(parts), reverse=True)), re.I) if parts else None


def hidden(value: Any, secrets_: list[str], tokens: tuple[str, ...] = ()) -> Any:
    """What comes from the page, without the login that was filled in. (A site that wants to can always write it in some
    other way: it has it. This keeps ordinary pages from showing it.) `tokens`: long random values, as they are."""
    rx, ends = hider(secrets_), hider(secrets_, cut_off=True)

    def other(m: re.Match) -> str:  # written in a way not in the list: decode, and then leave nothing of it
        x = m.group()
        return "•••" if any(c in x for c in "%&+") and any(rx.search(d) for d in (
            urllib.parse.unquote(x), urllib.parse.unquote_plus(x), html.unescape(x), urllib.parse.unquote(urllib.parse.unquote(x)))) else x

    def cut(x: str) -> str:
        m = re.match(r"https?://[^/?#\s]*", x)  # a site's name isn't a password, even when one is spelled the same
        start = m.end() if m else 0
        if tokens:
            rest = x[start:]
            for t in tokens:
                rest = rest.replace(t, "•••")
                at = rest.rfind(t[:16])
                if at >= 0 and t.startswith(rest[at:]):  # the beginning of one, where the text was cut
                    rest = rest[:at] + "•••"
            x = x[:start] + rest
        if not rx:
            return x
        return x[:start] + re.sub(r"\S+", other, (ends if len(x) >= 40 else rx).sub("•••", x[start:]))

    def walk(x: Any, words: bool) -> Any:
        if isinstance(x, str):
            return cut(x) if words else x
        if isinstance(x, list):
            return [walk(v, words) for v in x]
        if isinstance(x, dict):
            # ("bound" is what ampi-signin hands back to be compared; it goes nowhere else.)
            return {k: v if k == "bound" else walk(v, words if k not in PAGE_WORDS else True) for k, v in x.items()}
        return x
    return walk(value, False) if rx or tokens else value


def origin_of(url: str, security_origin: str | None = None) -> str:
    """'https://shop.example' or 'opaque' (about:, data:, blob:, javascript:, srcdoc, 'null')."""
    u = (url or "").strip().lower()
    if security_origin in ("null", "") or u.startswith(("about:", "data:", "blob:", "javascript:", "filesystem:")):
        return "opaque"
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "opaque"
    default = {"http": 80, "https": 443}[p.scheme]
    try:
        port = p.port
    except ValueError:
        return "opaque"
    host = p.hostname if ":" not in p.hostname else f"[{p.hostname}]"
    origin = f"{p.scheme}://{host}" + (f":{port}" if port and port != default else "")
    if security_origin and security_origin.rstrip("/").lower() != origin:
        return "opaque"  # the engine says the document's origin isn't its URL's (sandboxed, inherited…): don't trust it
    return origin


def listener_uid(port: int, paths: tuple[str, ...] = ("/proc/net/tcp",)) -> int | None:
    """The uid owning the LISTEN socket on 127.0.0.1:port (or 0.0.0.0:port), from /proc/net/tcp."""
    want = {f"0100007F:{port:04X}", f"00000000:{port:04X}"}
    owners = set()
    for path in paths:
        try:
            with open(path, encoding="ascii") as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 7 and parts[1].upper() in want and parts[3] == "0A":
                        owners.add(int(parts[7]))
        except OSError:
            continue
    return owners.pop() if len(owners) == 1 else None


def mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 0


# --- CDP over a minimal WebSocket client -----------------------------------------------------------


class WebSocket:
    def __init__(self, host: str, port: int, path: str, timeout: float = CALL_TIMEOUT):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk or len(buf) > 65536:
                raise CdpError("handshake failed")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CdpError("handshake refused")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
                                if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 64 * 1024 * 1024:
                raise CdpError("frame too big")
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 8:
                raise CdpError("connection closed")
            if op == 9:  # ping
                continue
            if op in (0, 1, 2):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Cdp:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.n = 0

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        self.n += 1
        msg: dict[str, Any] = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.sock.settimeout(timeout)
        try:
            self.ws.send(json.dumps(msg))
            while True:
                r = json.loads(self.ws.recv())
                if r.get("id") == self.n:  # events (console, network, …) are dropped unread
                    if "error" in r:
                        raise CdpProtocolError(f"{method} failed")
                    return r.get("result") or {}
        except (OSError, ValueError) as e:
            raise CdpError("engine connection failed") from e

    def close(self) -> None:
        self.ws.close()


ENGINE_WAIT = 15  # seconds for the engine's listener to appear after (re)start


def connect_engine(engine_uid: int | None, proc_paths: tuple[str, ...] = ("/proc/net/tcp",),
                   wait: float = ENGINE_WAIT, sleep: Callable[[float], None] = time.sleep, port: int = CDP_PORT) -> Cdp:
    """Only to the engine's own listener: refuse at once if anything else owns the port (a squatter); while
    nothing listens yet (the engine is starting with the controller), wait a little."""
    if engine_uid is None:
        raise Refused("the checkout browser isn't set up (no engine user)")
    waited = 0.0
    while True:
        owner = listener_uid(port, proc_paths)
        if owner == engine_uid:
            break
        if owner is not None or waited >= wait:
            raise Refused("the checkout browser isn't running right now; try again in a minute")
        sleep(0.5)
        waited += 0.5
    conn = http.client.HTTPConnection(CDP_HOST, port, timeout=5)
    try:
        conn.request("GET", "/json/version")
        info = json.loads(conn.getresponse().read(65536) or b"{}")
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise CdpError("engine unreachable") from e
    finally:
        conn.close()
    path = urllib.parse.urlsplit(str(info.get("webSocketDebuggerUrl", ""))).path or "/"
    return Cdp(WebSocket(CDP_HOST, port, path))


# --- the page-side script (runs in each frame's isolated world) ------------------------------------

# One op per call: __ampi.run({...}) returns a JSON string. Elements live in a Map in this world only.
PAGE_JS = r"""
(function () {
  const G = globalThis;
  if (!G.__ampi) {
    const els = new Map(), ids = new WeakMap(), kept = new WeakSet();  // kept: fields a password was put into
    let next = 1;
    const txt = (s, n) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim().slice(0, n || 120);
    const shown = (n) => {
      if (n.hidden) return false;
      try { const st = getComputedStyle(n); if (st && (st.display === 'none' || st.visibility === 'hidden')) return false; } catch (e) {}
      return true;
    };
    const visible = (el) => {  // the element and its ancestors (a hidden mega-menu hides its links)
      if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') return false;
      let n = el, depth = 0;
      while (n && n.nodeType === 1 && depth < 40) { if (!shown(n)) return false; n = n.parentElement; depth++; }
      return true;
    };
    const maxOf = (el) => parseInt(el.getAttribute('maxlength') || '', 10) || 0;  // (one engine has no el.maxLength)
    const disabledOf = (el) => !!el.disabled || el.getAttribute('aria-disabled') === 'true';
    const kindOf = (el) => {
      const t = el.tagName, ty = (el.type || '').toLowerCase(), role = el.getAttribute('role');
      if (t === 'INPUT') {
        if (ty === 'checkbox') return 'checkbox';
        if (ty === 'radio') return 'radio';
        if (['submit', 'button', 'image', 'reset'].includes(ty)) return 'button';
        return 'input';
      }
      if (t === 'TEXTAREA') return 'textarea';
      if (t === 'SELECT') return 'select';
      if (role === 'switch' || role === 'checkbox') return 'checkbox';
      if (['option', 'gridcell', 'tab', 'menuitem', 'radio'].includes(role) && t !== 'A') return 'button';
      if (t === 'BUTTON' || role === 'button') return 'button';
      if (t === 'A' && el.hasAttribute('href')) return 'link';
      return null;
    };
    const isChecked = (el) => el.tagName === 'INPUT' ? !!el.checked : el.getAttribute('aria-checked') === 'true';
    const labelOf = (el, kind) => {
      let s = el.getAttribute('aria-label') || '';
      const lb = el.getAttribute('aria-labelledby');
      if (!s && lb) s = lb.split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.textContent : ''; }).join(' ');
      if (!s && el.id) { try { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) s = l.textContent; } catch (e) {} }
      if (!s) { const l = el.closest && el.closest('label'); if (l) s = l.textContent.replace(el.textContent || '', ''); }
      if (!s && kind === 'button' && el.tagName === 'INPUT') s = el.value;  // a button's caption, not user input
      if (!s && (kind === 'button' || kind === 'link')) s = el.textContent;
      if (!s) s = el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
      return txt(s);
    };
    const idOf = (el) => { let i = ids.get(el); if (!i) { i = next++; ids.set(el, i); els.set(i, el); } return i; };
    const describe = (el) => {
      const kind = kindOf(el);
      const d = { n: idOf(el), kind: kind, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
        autocomplete: txt(el.getAttribute('autocomplete'), 60), name: txt(el.getAttribute('name'), 80),
        id: txt(el.id, 80), label: labelOf(el, kind), required: !!el.required, checked: null, filled: false,
        disabled: disabledOf(el), visible: visible(el), max: maxOf(el),
        role: txt(el.getAttribute('role'), 30) };
      for (const k of ['aria-selected', 'aria-pressed', 'aria-checked'])
        if (kind === 'button' && el.hasAttribute(k)) d.checked = el.getAttribute(k) === 'true' || !!d.checked;
      if (kind === 'checkbox' || kind === 'radio') d.checked = isChecked(el);
      if (kind === 'input' || kind === 'textarea') d.filled = (el.value || '') !== '';
      if (kind === 'select') { d.options = Array.from(el.options).slice(0, 30).map(o => txt(o.text, 60));
        const o = el.options[el.selectedIndex]; d.selected = o ? txt(o.text, 60) : null; }
      return d;
    };
    const formOf = (e) => e.form instanceof HTMLFormElement ? e.form : (e.closest ? e.closest('form') : null);
    const resolve = (u) => { try { return String(new URL(u || '', document.baseURI || location.href)); } catch (e) { return 'unreadable'; } };
    const offside = (el) => {  // moved out of the page, or see-through
      for (let n = el, i = 0; n && n.nodeType === 1 && i < 4; n = n.parentElement, i++) {
        try { const st = getComputedStyle(n);
          if (st && (st.opacity === '0' || ['left', 'top', 'textIndent'].some(k => parseFloat(st[k]) <= -500))) return true; } catch (e) {}
      }
      return false;
    };
    // Content of somebody else that is kept apart only by sandbox="…" (a comment, an ad in a srcdoc): an engine that doesn't
    // know sandbox would let its scripts read this page.
    const boxed = (doc, depth) => Array.from(doc.querySelectorAll('iframe,frame,object,embed')).some(f => {
      const src = (f.getAttribute('src') || f.getAttribute('data') || '').trim();
      if (f.hasAttribute('sandbox')) {
        if (f.hasAttribute('srcdoc') || !src || /^(about|blob|data|javascript|filesystem):/i.test(src)) return true;
        try { if (new URL(src, document.baseURI || location.href).origin === location.origin) return true; } catch (e) { return true; }
      }
      let inner = null;
      try { inner = f.contentDocument; } catch (e) {}
      return !!inner && (depth > 3 || boxed(inner, depth + 1));
    }) || Array.from(doc.querySelectorAll('*')).some(e => e.shadowRoot && (depth > 8 || boxed(e.shadowRoot, depth + 1)));
    const loginFacts = (U, S, C) => {  // C: a one-time code in the same form
      const F = formOf(S || U);
      let box = F;
      if (!F) { box = S || U; while (box && !(box.contains(S || U) && box.contains(U || S))) box = box.parentElement; box = box || document; }
      // Every control of the form, also those that belong to it from elsewhere on the page (form="…").
      const all = Array.from(box.querySelectorAll ? box.querySelectorAll('input,select,textarea,[contenteditable=""],[contenteditable=true]') : []);
      if (S && !all.includes(S)) all.push(S);
      if (F && F.id) for (const e of document.querySelectorAll('input[form],select[form],textarea[form]')) if (formOf(e) === F && !all.includes(e)) all.push(e);
      let extra = false, boxes = 0, passwords = 0;
      for (const e of all) {
        if (F && formOf(e) !== F && e.form !== undefined) continue;
        const ty = e.tagName === 'INPUT' ? (e.type || '').toLowerCase() : '';
        // A field nobody sees or reaches, left empty: a trap for robots.
        const trap = e !== S && e !== U && e !== C && e.tagName === 'INPUT' && ['password', 'text', 'email', 'tel', 'url', ''].includes(ty) && !e.value &&
          (e.getAttribute('tabindex') === '-1' || !!(e.closest && e.closest('[aria-hidden=true]')) || offside(e));
        if (ty === 'password' && visible(e) && !trap && e !== C) passwords++;
        if (e === U || e === S || e === C || ['hidden', 'submit', 'button', 'image', 'reset'].includes(ty)) continue;
        if (ty === 'password') { if ((!visible(e) || trap) && !e.value) continue; }
        else if (ty === 'checkbox') { boxes++; continue; }
        else if (trap) continue;
        else if (e.tagName === 'INPUT' && !visible(e) && ['text', 'email', 'tel', ''].includes(ty)) continue;  // a trap for robots, or a copy of the name
        else if (e.tagName === 'SELECT' && /lang|locale|country|region|dial|prefix/i.test((e.getAttribute('name') || '') + ' ' + e.id)) continue;
        else if (e.tagName === 'INPUT' && (e.readOnly || e.disabled) && ['text', 'email', 'tel', ''].includes(ty)) continue;  // who is signing in
        else if (e.tagName === 'SELECT' || e.tagName === 'TEXTAREA') { if (!visible(e)) continue; }
        extra = true;
      }
      return { connected: true, hasForm: !!F, sameForm: [U, S, C].every(e => !e || formOf(e) === F) && (!C || !!F || box.contains(C)), passwords: passwords, extra: extra, boxes: boxes,
        shown: [U, S, C].every(e => !e || visible(e)), method: F ? (F.getAttribute('method') || '').toLowerCase().trim() : '',
        action: F ? actionOf(F) : '', boxed: boxed(document, 0) };
    };
    // Where a form goes: '' = nowhere or the page itself (an empty action means the page, whatever its <base> says).
    const actionOf = (F) => { const raw = (F.getAttribute('action') || '').trim(); return !raw || /^javascript:/i.test(raw) ? '' : resolve(raw).slice(0, 1000); };
    // A one-time code: one field, or 4 to 10 of one character each. `box`: the form, or what holds the field and a button.
    const codeBox = (C) => {
      const F = formOf(C); if (F) return F;
      let b = C.parentElement, i = 0;
      while (b && b !== document.body && i < 8 && !b.querySelector('button,input[type=submit],[role=button]')) { b = b.parentElement; i++; }
      if (b && b !== document.body) return b;
      for (b = C, i = 0; i < 2 && b.parentElement && b.parentElement !== document.body; i++) b = b.parentElement;  // no button anywhere near
      return b === C ? C.parentElement || document.body : b;
    };
    const codeInputs = (C) => {
      const F = formOf(C), box = codeBox(C);
      const all = Array.from(box.querySelectorAll('input,select,textarea,[contenteditable=""],[contenteditable=true]'));
      if (F && F.id) for (const e of document.querySelectorAll('input[form],select[form],textarea[form]')) if (formOf(e) === F && !all.includes(e)) all.push(e);
      return all.filter(e => !F || e.form === undefined || formOf(e) === F);
    };
    const oneChar = (e) => maxOf(e) === 1 || e.getAttribute('max') === '9' || /^(\\d|\[0-9\])(\{1\})?\*?$/.test(e.getAttribute('pattern') || '') ||
      /(digit|character|number) ?\d/i.test(e.getAttribute('aria-label') || '');
    const codeFields = (C, all) => {
      const ty = (C.type || '').toLowerCase();
      const c = all.filter(e => e.tagName === 'INPUT' && (e.type || '').toLowerCase() === ty && visible(e) && !e.readOnly && (!e.disabled || oneChar(e)));  // (boxes often wake up one after the other)
      return c.length >= 4 && c.length <= 10 && c[0] === C && c.filter(oneChar).length >= c.length - 1 ? c : [C];
    };
    const codeFacts = (C, P) => {
      const F = formOf(C), box = codeBox(C), all = codeInputs(C), cs = codeFields(C, all);
      let extra = false, boxes = 0;
      for (const e of all) {
        const ty = e.tagName === 'INPUT' ? (e.type || '').toLowerCase() : '';
        if (cs.includes(e) || ['hidden', 'submit', 'button', 'image', 'reset'].includes(ty)) continue;
        if (ty === 'checkbox') { boxes++; continue; }
        if (e.tagName === 'INPUT' && e.value && (ty === 'email' || e.value.includes('@') || /username|email/.test((e.getAttribute('autocomplete') || '').toLowerCase()))) continue;  // who is signing in
        if (e.tagName === 'INPUT' && !['radio', 'file'].includes(ty) && (!visible(e) || e.readOnly || e.disabled || offside(e) || e.getAttribute('tabindex') === '-1')) continue;
        if (!visible(e)) continue;
        extra = true;
      }
      let press = '';
      if (P) {
        const g = gateOf(P, C);
        press = g.submits && g.withForm ? 'form' : !F && !g.inForm && box.contains(P) && ['button', 'input'].includes(g.tag) ? 'box' : '';
      }
      return { connected: true, hasForm: !!F, count: cs.length, max: cs.length > 1 ? cs.length : maxOf(C), type: (C.type || '').toLowerCase(),
        extra: extra, boxes: boxes, shown: cs.every(visible), method: F ? (F.getAttribute('method') || '').toLowerCase().trim() : '',
        action: F ? actionOf(F) : '', boxed: boxed(document, 0), press: press };
    };
    // The words around it, without what links and buttons say ("Change number" next to a code is not what it is for).
    const codeWords = (C) => {
      const skip = ['A', 'BUTTON', 'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SELECT', 'INPUT', 'TEXTAREA', 'NAV', 'FOOTER'];
      const says = (root, limit) => {
        let out = '';
        const go = (n) => {
          if (out.length > limit * 2) return;
          if (n.nodeType === 3) { out += ' ' + n.nodeValue; return; }
          if (n.nodeType !== 1 || skip.includes(n.tagName) || n.getAttribute('role') === 'button' || !shown(n)) return;
          for (const c of n.childNodes) go(c);
        };
        go(root); return txt(out, limit);
      };
      const box = codeBox(C);
      let around = C.closest('dialog,[role=dialog],section,article,main,[role=main]'), above = '';
      if (!around) {
        for (let n = box, i = 0; i < 3 && n.parentElement && n.parentElement !== document.body && n !== document.body; i++) around = n = n.parentElement;
      }
      const before = (from) => { for (let n = from.previousElementSibling, i = 0; n && i < 4; n = n.previousElementSibling, i++) if (n.tagName !== 'HEADER') above += ' ' + says(n, 500); };
      before(box); if (around) before(around);
      const hs = (r) => Array.from(r.querySelectorAll('h1,h2,h3,h4,[role=heading],legend')).filter(h => visible(h) && !h.closest('footer,nav,aside') && (r !== document || !h.closest('header')))
        .map(h => txt(h.textContent, 200));
      const near = around ? hs(around) : [], heads = near.concat(hs(document).filter(h => !near.includes(h))).slice(0, 30);
      return [txt(document.title, 200) + ' \n ' + txt(heads.join(' \n '), 2500), says(box, 1500) + ' \n ' + (around ? says(around, 3000) : '') + ' \n ' + above];
    };
    // What the buttons say that would send it.
    const codeCaptions = (C) => {
      const F = formOf(C), out = [];
      const all = Array.from(codeBox(C).querySelectorAll('button,input[type=submit],input[type=image],[role=button]'));
      if (F && F.id) all.push(...document.querySelectorAll('button[form],input[form]'));
      for (const b of all) {
        const ty = b.tagName === 'BUTTON' ? (b.getAttribute('type') || 'submit').toLowerCase() : (b.type || '').toLowerCase();
        if (F && (formOf(b) !== F || !['submit', 'image'].includes(ty))) continue;
        if (out.length < 10) out.push(labelOf(b, 'button') + ' ' + txt(b.getAttribute('formaction'), 200));
      }
      return out;
    };
    const setValue = (el, v) => {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      try { el.dispatchEvent(new FocusEvent('focus')); } catch (e) {}
      setter.call(el, v);
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: v, inputType: 'insertText' }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      try { el.dispatchEvent(new FocusEvent('blur')); } catch (e) {}
    };
    const gateOf = (el, other) => {
        const lab = el.closest('label');
        const c = el.closest('button,input,a[href],[role=button]') || (lab && lab.control) || el;
        const texts = [];
        for (const n of (c === el ? [el] : [el, c])) {
          const by = (n.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).map(i => { const x = document.getElementById(i); return x ? x.textContent : ''; }).join(' ');
          for (const s of [n.getAttribute('aria-label'), by, n.tagName === 'INPUT' ? (['submit', 'button', 'image', 'reset'].includes((n.type || '').toLowerCase()) ? n.value : '') : n.textContent, n.getAttribute('title'), n.getAttribute('alt')]) {
            const t = txt(s, 200); if (t && !texts.includes(t)) texts.push(t);
          }
          n.querySelectorAll && n.querySelectorAll('img[alt]').forEach(i => { const t = txt(i.getAttribute('alt'), 100); if (t && texts.length < 12 && !texts.includes(t)) texts.push(t); });
        }
        const owner = c.form instanceof HTMLFormElement ? c.form : (c.closest ? c.closest('form') : null);
        const cty = c.tagName === 'BUTTON' ? (c.getAttribute('type') || 'submit').toLowerCase() : (c.type || '').toLowerCase();
        const presses = (c.tagName === 'BUTTON' || c.tagName === 'INPUT') && ['submit', 'image'].includes(cty);
        const submits = !!owner && presses;
        let search = false;
        if (owner) {
          const fs = Array.from(owner.querySelectorAll('input,textarea,select')).filter(i => !['hidden', 'submit', 'button', 'image', 'reset'].includes((i.type || '').toLowerCase()));
          search = fs.length <= 1 && ((owner.getAttribute('role') || '') === 'search' || !!owner.closest('[role=search]') || (fs.length === 1 && (fs[0].type === 'search' || /^(q|s|query|search|keyword)$/i.test(fs[0].getAttribute('name') || ''))));
        }
        let dest = '', method = '';
        if (c.tagName === 'A') { dest = resolve(c.getAttribute('href')); method = 'link'; }
        else if (owner) { dest = resolve(c.getAttribute('formaction') || owner.getAttribute('action'));
          method = (c.getAttribute('formmethod') || owner.getAttribute('method') || 'get').toLowerCase(); }
        let box = owner || c.closest('[role=dialog],dialog,section,article,main') || c.parentElement;
        const own = owner && (c.getAttribute('formmethod') || owner.getAttribute('method'));
        // A link that is only a link (its address says what it does), a box of a form that has a button, closing a dialog.
        const plain = c.tagName === 'A' && el.tagName === 'A' && !c.hasAttribute('onclick') && !c.hasAttribute('role') &&
          !Array.from(c.attributes || []).some(x => /^(on|data-.*(method|confirm|action|remote|toggle|ajax|click|submit|href|url))/i.test(x.name));
        const formButton = !!owner && Array.from(owner.querySelectorAll('button,input[type=submit],input[type=image]')).some(x =>
          (x.tagName === 'INPUT' || (x.getAttribute('type') || 'submit').toLowerCase() === 'submit') && visible(x));
        // (Not the whole page: cookie tools mark <body> too.)
        const about = c.closest ? c.closest('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i]') : null;
        const cookies = about && !['HTML', 'BODY', 'MAIN', 'FORM'].includes(about.tagName) && String(about.textContent || '').length < 4000;
        const dialog = cookies ? 'cookies' : c.closest && c.closest('[role=dialog],[role=alertdialog],dialog,[aria-modal=true]') ? 'dialog' : '';
        // A password that was filled in and would go into an address.
        const eff = (c.getAttribute('formmethod') || (owner && owner.getAttribute('method')) || 'get').trim().toLowerCase();
        const leaks = submits && eff !== 'post' && Array.from(owner.querySelectorAll('input')).concat(owner.id ? Array.from(document.querySelectorAll('input[form]')) : []).some(x => kept.has(x) && x.value && formOf(x) === owner);
        return { connected: true, plain: plain, formButton: formButton, dialog: dialog, leaks: !!leaks, inForm: !!owner, ownMethod: !!own, to: c.hasAttribute('formaction') ? resolve(c.getAttribute('formaction')) : owner ? actionOf(owner) : '', withForm: !!(other && owner && formOf(other) === owner), withBox: !!(other && codeBox(other).contains(c)), tag: c.tagName.toLowerCase(), texts: texts.slice(0, 12), ident: txt((c.getAttribute('name') || '') + ' ' + c.id, 120),
          submits: submits, search: search, dest: dest.slice(0, 1000), method: method, href: String(location.href),
          around: txt(box ? (box.innerText || box.textContent) : '', 600) };
    };
    G.__ampi = { run: function (a) {
      const get = (n) => { const el = els.get(n); return el && el.isConnected ? el : null; };
      if (a.op === 'enumerate') {
        const list = [];
        // Form controls first, links after: on big shops the cap must not cut off the inputs and buttons.
        for (const sel of ['input,textarea,select,button,[role=button],[role=switch],[role=checkbox],[role=option],[role=gridcell],[role=tab],[role=menuitem],[role=radio]', 'a[href]']) {
          for (const el of document.querySelectorAll(sel)) {
            if (list.length >= 400) break;
            if (kindOf(el) && visible(el)) list.push(describe(el));
          }
        }
        // Every checked box, shown or not (a hidden one is still sent with the form), for the risky check.
        const checked = [];
        for (const el of document.querySelectorAll('input[type=checkbox],[role=switch],[role=checkbox]')) {
          if (checked.length >= 100) break;
          if (isChecked(el)) checked.push(labelOf(el, 'checkbox'));
        }
        const out = { href: String(location.href), ready: document.readyState, elements: list, checked: checked };
        let body = '';
        if (document.body) {  // without script/style text (the engine's innerText includes it)
          body = String(document.body.innerText || '');
          document.body.querySelectorAll('script,style,noscript,template').forEach(n => {
            const t = (n.textContent || '').trim(); if (t) body = body.split(t).join('');
          });
        }
        if (a.main) { out.title = txt(document.title, 300); out.text = body.slice(0, 200000); }
        else { out.text = body.slice(0, 4000); }
        return out;
      }
      if (a.op === 'form') {  // what would be sent (bind_submit/submit). Secret values never leave the page.
        const out = [], hidden = []; let problem = '';
        const sel = 'input,textarea,select,[contenteditable=""],[contenteditable=true],[role=switch],[role=checkbox],[role=radio],' +
          '[role=option],[role=gridcell],[role=tab],[aria-selected=true],[aria-pressed=true],[aria-checked=true]';
        for (const n of document.querySelectorAll('*'))
          if (n.shadowRoot && n.shadowRoot.querySelector('input,textarea,select')) problem = 'a form inside a web component';
        for (const el of document.querySelectorAll(sel)) {
          const ty = (el.type || '').toLowerCase(), tag = el.tagName;
          if (tag === 'INPUT' && ['submit', 'button', 'image', 'reset'].includes(ty)) continue;
          if (tag === 'INPUT' && ty === 'file') { if (el.files && el.files.length) problem = 'a file to upload'; continue; }
          const name = txt(el.getAttribute('name'), 80);
          if (tag === 'INPUT' && ty === 'hidden') { hidden.push([name, String(el.value || '').slice(0, 20000)]); continue; }
          const kind = kindOf(el) || 'text', ac = (el.getAttribute('autocomplete') || '').toLowerCase();
          const secret = ty === 'password' || kept.has(el) || /(^| )(cc-|one-time-code|current-password|new-password)/.test(ac) ||
            /cvc|cvv|csc|iban|passw|\botp\b/i.test(name + ' ' + el.id);
          let v = null;
          if (tag === 'SELECT') v = Array.from(el.options).filter((o, i) => o.selected || (!el.multiple && i === el.selectedIndex)).map(o => txt(o.text, 200) + (txt(o.text, 200) === txt(o.value, 200) ? '' : ' (' + txt(o.value, 200) + ')')).join('; ');
          else if (tag === 'INPUT' && (ty === 'checkbox' || ty === 'radio')) { if (el.checked) v = true; }
          else if (tag === 'INPUT' || tag === 'TEXTAREA') v = String(el.value || '');
          else if (['aria-selected', 'aria-pressed', 'aria-checked'].some(k => el.getAttribute(k) === 'true')) v = true;
          else if (el.hasAttribute('contenteditable')) v = String(el.textContent || '').trim();
          if (v === null || v === '') continue;
          if (secret) v = '(secret)';
          if (v !== true && v.length > 1000) problem = 'a very long text';
          if (out.length >= 60) { problem = 'too many filled fields'; break; }
          out.push({ label: labelOf(el, v === true ? 'button' : kind), kind: kind, value: v, shown: visible(el), name: name, type: ty });
        }
        return { fields: out, hidden: hidden, problem: problem };
      }
      if (a.op === 'gate') {  // what a press on this element would really press
        const el = get(a.n); if (!el) return { connected: false };
        return gateOf(el, a.with ? get(a.with) : null);
      }
      if (a.op === 'inspect') { const el = get(a.n); return el ? Object.assign({ connected: true }, describe(el)) : { connected: false }; }
      if (a.op === 'login' || a.op === 'signin') {  // what the fields of a sign-in form belong to; signin: and fill them
        const U = a.user ? get(a.user) : null, S = a.secret ? get(a.secret) : null, C = a.code ? get(a.code) : null;
        if ((a.user && !U) || (a.secret && !S) || (a.code && !C)) return { connected: false };
        const facts = loginFacts(U, S, C);
        if (a.op === 'login') return facts;  // (below: set = whether anything was put in)
        const same = () => JSON.stringify(Object.keys(a.expect).map(k => loginFacts(U, S, C)[k])) === JSON.stringify(Object.values(a.expect));
        if (!same()) return { ok: false, set: false };
        if (U) setValue(U, a.values.user);  // the page's own scripts run here
        if (S) {
          if (!S.isConnected || S.type !== 'password' || !same()) return { ok: false };
          kept.add(S);
          setValue(S, a.values.password);
        }
        if (C) {
          if (!C.isConnected || !same()) return { ok: false };
          kept.add(C);
          setValue(C, a.values.code);
        }
        // A form that names no method would be sent with GET, the login in its address, should its script not run here.
        const F = formOf(S || U);
        if (F && !F.getAttribute('method')) F.setAttribute('method', 'post');
        return { ok: true };
      }
      if (a.op === 'code') {  // what the field for a one-time code belongs to
        const C = get(a.n), P = a.press ? get(a.press) : null;
        return !C || (a.press && !P) ? { connected: false } : Object.assign(codeFacts(C, P), { words: codeWords(C).join(' \n '), heads: codeWords(C)[0], captions: codeCaptions(C) });
      }
      if (a.op === 'setcode') {  // one field, or one box after the other (a page's script sees each before the next)
        const C = get(a.n), P = a.press ? get(a.press) : null;
        if (!C || (a.press && !P)) return { ok: false, set: a.i > 0 };
        if (a.i === 0) {
          const f = codeFacts(C, P);
          for (const k of Object.keys(a.expect)) if (JSON.stringify(f[k]) !== JSON.stringify(a.expect[k])) return { ok: false, set: false };
          const F = formOf(C); if (F && !F.getAttribute('method')) F.setAttribute('method', 'post');
          G.__ampi.boxes = codeFields(C, codeInputs(C));
          if (a.look) return { ok: true, full: G.__ampi.boxes.map((e, i) => e.value ? i : -1).filter(i => i >= 0) };
        }
        const e = (G.__ampi.boxes || [])[a.i];
        if (!e || !e.isConnected || !visible(e)) return { ok: false, set: a.i > 0 };
        if (a.value) kept.add(e);
        setValue(e, a.value); return { ok: true };
      }
      if (a.op === 'clickif') {  // press it only if it still is what was decided about
        const el = get(a.n); if (!el) return { ok: false };
        if (disabledOf(el)) return { ok: false };
        if (a.filled) { const c = get(a.filled); if (!c || !visible(c) || !c.value) return { ok: false }; }
        const g = gateOf(el, null);
        for (const k of Object.keys(a.expect)) if (k !== 'method' && JSON.stringify(g[k]) !== JSON.stringify(a.expect[k])) return { ok: false };
        const b = el.closest('button,input') || el, F = formOf(b), m = F ? (b.getAttribute('formmethod') || F.getAttribute('method') || '').trim().toLowerCase() : 'post';
        if (m && m !== 'post') return { ok: false };
        if (!m) F.setAttribute('method', 'post');  // (never with GET: the login would be in the address)
        el.click(); return { ok: true };
      }
      if (a.op === 'set') { const el = get(a.n); if (!el) return { ok: false }; if (a.keep) kept.add(el); setValue(el, a.value); return { ok: true }; }
      if (a.op === 'click') { const el = get(a.n); if (!el) return { ok: false };
        try { el.scrollIntoView && el.scrollIntoView({ block: 'center' }); } catch (e) {} el.click(); return { ok: true }; }
      if (a.op === 'choose') { const el = get(a.n); if (!el || el.tagName !== 'SELECT') return { ok: false };
        const opts = Array.from(el.options); let i = opts.findIndex(o => txt(o.text, 200) === a.option);
        if (i < 0) { const lo = a.option.toLowerCase(); const m = opts.map((o, k) => [k, txt(o.text, 200).toLowerCase()]).filter(x => x[1] === lo);
          if (m.length === 1) i = m[0][0]; }
        if (i < 0) return { ok: false, why: 'option' };
        el.selectedIndex = i; el.dispatchEvent(new Event('input', { bubbles: true })); el.dispatchEvent(new Event('change', { bubbles: true }));
        return { ok: true }; }
      if (a.op === 'state') return { href: String(location.href), ready: document.readyState };
      if (a.op === 'back') { history.back(); return { ok: true }; }
      return { error: 'op' };
    } };
  }
})();
"""


def page_call(op: dict) -> str:
    return PAGE_JS + "\nJSON.stringify(globalThis.__ampi.run(" + json.dumps(op) + "));"


# --- sessions ------------------------------------------------------------------------------------


class Session:
    def __init__(self, sid: str, cdp: Cdp, context: str, target: str, cdp_session: str, now: float):
        self.id, self.cdp, self.context, self.target, self.cdp_session = sid, cdp, context, target, cdp_session
        self.started = self.last = now
        self.engine = "obscura"
        self.hold_until = 0.0
        self.readonly = False
        self.gen = 0         # counts everything done in the session: an approved press needs it unchanged
        self.filled = False  # the owner's details were filled in
        self.secrets: list[str] = []  # the login filled in: cut out of every reply
        self.tokens: tuple[str, ...] = ()  # of a session the owner gave: cut out of every reply, too
        self.code: list[tuple[Code, str, int, float]] = []  # one-time codes filled in, the document and field each is in, and since when
        self.salt = secrets.token_bytes(16)
        self.seen_account = False  # a page offered to sign out
        # A one-time code goes in only where a sign-in itself led, and that began at the site's own start or sign-in page: `chain` is
        # `gen` as it was then, carried on by what belongs to signing in (_leads_to_login, fill_login, _carries). Anything else that
        # was done breaks it, for good: before the sign-in, too (a site remembers where one wanted to go).
        self.chain: int | None = None
        self.chain_at = 0.0
        self.broke = ""  # why it ended, for Ampi
        self.steps = self.codes = 0
        self.step_form: tuple[str, int, list[str]] = ("", 0, [])  # (document, number, what it is) of the field filled in last: its form's button is a step
        self.code_place: list | None = None  # where the first code went: another try only there
        self.user_step = False  # the first page of a sign-in was filled in (its user name)
        self.signed_in = False  # a password was filled in
        self.full = False  # it was on more sites than are counted
        self.typed: set[str] = set()    # documents (loader ids) something else was typed into
        self.visited: set[str] = set()  # sites the page was on
        self.frame_names: dict[str, str] = {}      # CDP frameId -> "f0", "f1", …
        self.worlds: dict[str, tuple[str, int]] = {}  # CDP frameId -> (loaderId, isolated world context id)

    @property
    def account(self) -> bool:
        """Perhaps signed in to the owner's account (some sites need no more than the name, or a link from an email)."""
        return self.login_used or self.seen_account

    @property
    def login_used(self) -> bool:
        return self.signed_in or self.user_step

    @property
    def hide(self) -> list[str]:
        return [*self.secrets, *(c[0] for c in self.code)]

    def cut(self, value: Any) -> Any:
        return hidden(value, self.hide, self.tokens)

    def name(self, frame_id: str) -> str:
        if frame_id not in self.frame_names:
            self.frame_names[frame_id] = f"f{len(self.frame_names)}"
        return self.frame_names[frame_id]

    def frame_id(self, name: str) -> str | None:
        return next((k for k, v in self.frame_names.items() if v == name), None)


class Controller:
    def __init__(self, connect: Callable[[], Cdp] | None = None, clock: Callable[[], float] = time.time,
                 mem_mb: Callable[[], int] = mem_available_mb, check_url: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, engine_uid: int | None = None, signin: bool = False,
                 lp_uid: int | None = None):
        # With sign-ins, an engine is never used for a second session: what a page did to it must not see a login later.
        self.fresh = signin
        uids = {"obscura": engine_uid, "lightpanda": lp_uid}
        self._connect = (lambda engine: connect()) if connect else (
            lambda engine: connect_engine(uids[engine], port=ENGINES[engine], wait=ENGINE_WAIT if engine == "obscura" else 8))
        self.engines = {"obscura"} | ({"lightpanda"} if lp_uid is not None else set())
        self.clock, self.mem_mb, self.sleep = clock, mem_mb, sleep
        self.check_url = check_url or br.check_url
        self.session: Session | None = None
        # True once a session paid or lost its engine connection: when no session is left, the controller exits,
        # systemd stops the engine with it (BindsTo=), and the next request starts both fresh (no card details
        # left in a long-lived engine process).
        self.retire = False
        # Where a fill/pay got to, reported with every error: "verify" (nothing written: a definite refusal),
        # "filled" (values written, Pay not pressed), "sent" (Pay was pressed: outcome unknown).
        self._phase: str | None = None
        self._acted = False  # something was done to the page (or may have been)

    CHANGES = ("open", "click", "type", "choose", "back", "fill", "pay")
    SIGNIN = ("bind_login", "fill_login", "bind_code", "fill_code", "bind_session", "fill_session")  # for ampi-signin, and nothing else is

    # --- request entry -------------------------------------------------------------------

    def _error(self, text: str) -> dict:
        out: dict[str, Any] = {"ok": False, "error": text}
        if self._phase:
            out["phase"] = self._phase
        return out

    def handle(self, req: Any, signin: bool = False) -> dict:
        """`signin`: the caller is ampi-signin."""
        s = self.session
        out = self._handle(req, signin)
        both = [x for x in {s, self.session} if x is not None]
        secrets_, tokens = [x for session in both for x in session.hide], tuple(x for session in both for x in session.tokens)
        try:
            return hidden(out, secrets_, tokens) if secrets_ or tokens else out
        except Exception:
            self.retire = True
            self.drop()
            return {"ok": False, "error": "internal error in the checkout browser; the session was closed"}

    def _handle(self, req: Any, signin: bool) -> dict:
        self._phase, self._acted = None, False
        cmd = None
        try:
            self.expire()
            if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
                raise Refused("bad request")
            cmd = req["cmd"]
            if signin is not (cmd in self.SIGNIN):
                raise Refused("unknown command")
            fn = {"bind_login": self.bind_login, "fill_login": self.fill_login, "bind_code": self.bind_code, "fill_code": self.fill_code,
                  "bind_session": self.bind_session, "fill_session": self.fill_session,
                  "status": self.status, "open": self.open, "snapshot": self.snapshot_cmd, "click": self.click,
                  "type": self.type_text, "choose": self.choose, "back": self.back, "close": self.close,
                  "bind": self.bind, "gate": self.gate, "bind_submit": self.bind_submit, "submit": self.submit, "fill": self.fill, "pay": self.pay, "hold": self.hold}.get(cmd)
            if fn is None:
                raise Refused("unknown command")
            if cmd in self.CHANGES and self.session is not None:
                self.session.gen += 1
            out = fn(req)
            s = self.session
            if cmd in (*self.CHANGES, "submit") and s is not None and s.chain != s.gen and not s.broke:
                s.broke = ("the session began at an address that isn't plainly the site's start page or its sign-in page" if s.gen == 0 else
                           f"`checkout {cmd}` did something that isn't part of signing in")
            return out
        except Refused as e:
            s = self.session
            if cmd in self.CHANGES and s is not None and not self._acted and s.chain == s.gen - 1:
                s.chain = s.gen  # nothing was done
            return self._error(str(e))
        except br.Refused as e:  # check_url's messages name no values
            return self._error(str(e))
        except CdpProtocolError:  # the page was changing under us; the session stays
            return self._error(PAGE_CHANGED)
        except CdpError:
            self.retire = True
            self.drop()
            return self._error("the checkout browser failed; the session was closed")
        except Exception:  # never echo request contents
            self.retire = True
            self.drop()
            return self._error("internal error in the checkout browser; the session was closed")

    # --- lifecycle -----------------------------------------------------------------------

    def expire(self) -> None:
        s = self.session
        if s is None:
            return
        now = self.clock()
        if now - s.started > HARD_MAX or now > max(s.last + IDLE, s.hold_until):
            self.drop()

    def drop(self) -> None:
        s, self.session = self.session, None
        if s is None:
            return
        if s.readonly or s.secrets or s.signed_in or self.fresh or s.engine != "obscura":
            self.retire = True  # it paid, or signed in, or is the second engine: start over with a fresh one
        try:
            s.cdp.call("Target.disposeBrowserContext", {"browserContextId": s.context}, timeout=5)
        except Exception:
            pass
        try:
            s.cdp.close()
        except Exception:
            pass

    def _session(self, req: dict, writable: bool = True) -> Session:
        s = self.session
        if s is None or not isinstance(req.get("session"), str) or req["session"] != s.id:
            raise Refused("no such session")
        if writable and s.readonly:
            raise Refused("this session is finished (after paying only snapshot and close work)")
        s.last = self.clock()
        return s

    def status(self, req: dict) -> dict:
        s = self.session
        if s is None:
            return {"ok": True, "session": None, "state": "idle"}
        return {"ok": True, "session": s.id, "state": "readonly" if s.readonly else "active", "engine": s.engine, "codes": s.chain == s.gen,
                "started": s.started, "idle_for": round(self.clock() - s.last)}

    def hold(self, req: dict) -> dict:
        s = self._session(req, writable=False)
        until = req.get("until")
        if isinstance(until, bool) or not isinstance(until, (int, float)):
            raise Refused("bad hold")
        s.hold_until = min(float(until), s.started + HARD_MAX)
        return {"ok": True, "session": s.id, "until": s.hold_until}

    def close(self, req: dict) -> dict:
        self._session(req, writable=False)
        self.drop()
        return {"ok": True}

    def _new_session(self, engine: str) -> Session:
        if engine not in self.engines:
            raise Refused("the second browser engine isn't installed")
        self._ready_for(engine)
        cdp = self._connect(engine)
        try:
            ctx = cdp.call("Target.createBrowserContext", {"disposeOnDetach": True}).get("browserContextId")
            target = cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx}).get("targetId")
            sess = cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True}).get("sessionId")
            if not (ctx and target and sess):
                raise CdpError("no target")
            for m in ("Page.enable", "Runtime.enable"):
                cdp.call(m, {}, sess)
        except Exception:
            cdp.close()
            raise
        s = Session(secrets.token_hex(8), cdp, ctx, target, sess, self.clock())
        s.engine = engine
        self.session = s
        return s

    # --- navigation ----------------------------------------------------------------------

    def open(self, req: dict) -> dict:
        url = req.get("url")
        if not isinstance(url, str):
            raise Refused("give a link (https://…)")
        url = self.check_url(url)
        engine = req.get("engine")
        if engine is not None and (not isinstance(engine, str) or engine not in ENGINES):
            raise Refused("unknown browser engine")
        if req.get("session") is not None:
            s = self._session(req)
            if s.account:  # any address can be an action there
                raise Refused("signed in here: go on with the page's own links, or close the session")
            if engine is not None and engine != s.engine:
                raise Refused("this session runs in the other engine; close it first")
        elif self.session is not None:
            raise Refused("another checkout session is open; close it first")
        else:
            s = self._new_session(engine or "obscura")
            s.chain = s.gen if plain_entry(url) else None
        if s.chain == s.gen - 1 and plain_entry(url) and all(same_site(url, f"https://{v}/") for v in s.visited):
            s.chain = s.gen  # from the start page to the sign-in page
        self._acted = True
        self._visit(s, url)  # (also when it forwards at once)
        s.cdp.call("Page.navigate", {"url": url}, s.cdp_session)
        self._settle(s, NAV_TIMEOUT)
        return self._reply(s)

    def back(self, req: dict) -> dict:
        s = self._session(req)
        main = self._frames(s)[0]
        self._acted = True
        self._run(s, main, {"op": "back"})
        self._settle(s, NAV_TIMEOUT)
        return self._reply(s)

    def _settle(self, s: Session, limit: float) -> None:
        """Waits for the main frame to finish loading, then checks where it ended up."""
        deadline = self.clock() + limit
        self.sleep(0.5)
        while True:
            main = self._frames(s)[0]
            state = self._run(s, main, {"op": "state"}, quiet=True) or {}
            if state.get("ready") == "complete" or self.clock() >= deadline:
                break
            self.sleep(0.5)
        self.sleep(0.5)
        self._check_top(s)

    def _check_top(self, s: Session) -> None:
        top = self._frames(s)[0]
        if top["url"].startswith("about:blank"):
            return
        try:
            self.check_url(top["url"])
        except br.Refused:
            self.drop()
            raise Refused("the page went somewhere the checkout browser doesn't open; the session was closed") from None

    # --- frames, worlds, page calls ------------------------------------------------------

    def _frames(self, s: Session) -> list[dict]:
        tree = s.cdp.call("Page.getFrameTree", {}, s.cdp_session).get("frameTree") or {}
        out: list[dict] = []

        def walk(node: dict, parent: str | None) -> None:
            f = node.get("frame") or {}
            if f.get("id") and len(out) < MAX_FRAMES:
                url = str(f.get("url") or "")
                out.append({"frame_id": f["id"], "name": s.name(f["id"]), "url": url, "loader": f.get("loaderId", ""),
                            "origin": origin_of(url, f.get("securityOrigin")), "parent": parent})
            for c in node.get("childFrames") or []:
                walk(c, f.get("id"))

        walk(tree, None)
        if not out:
            raise CdpError("no frames")
        self._visit(s, out[0]["url"])
        return out

    @staticmethod
    def _visit(s: Session, url: str) -> None:
        site = (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
        if site and len(s.visited) < 50:
            s.visited.add(site)
        elif site and site not in s.visited:
            s.full = True

    def _world(self, s: Session, frame: dict) -> int:
        cached = s.worlds.get(frame["frame_id"])
        if cached and cached[0] == frame["loader"]:
            return cached[1]
        ctx = s.cdp.call("Page.createIsolatedWorld", {"frameId": frame["frame_id"], "worldName": "ampi"},
                         s.cdp_session).get("executionContextId")
        if not isinstance(ctx, int):
            raise CdpError("no world")
        s.worlds[frame["frame_id"]] = (frame["loader"], ctx)
        return ctx

    ONCE = ("click", "setcode", "signin", "clickif")  # what may have been done is never done again
    ELEMENT_OPS = ("inspect", "gate", "set", "click", "choose", "login", "signin", "clickif", "code", "setcode")

    def _eval(self, s: Session, frame: dict, op: dict) -> dict | None:
        ctx = self._world(s, frame)
        r = s.cdp.call("Runtime.evaluate", {"expression": page_call(op), "contextId": ctx, "returnByValue": True},
                       s.cdp_session)
        value = (r.get("result") or {}).get("value")
        try:
            data = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _run(self, s: Session, frame: dict, op: dict, quiet: bool = False) -> dict | None:
        """One page op in the frame's isolated world. An engine error (the context went away while the page
        navigated or a frame remounted) is retried once in a fresh world, but an element op only if the frame
        still holds the same document (its elements are gone otherwise). Connection loss raises CdpError."""
        try:
            return self._eval(s, frame, op)
        except CdpProtocolError:
            s.worlds.pop(frame["frame_id"], None)
            if op.get("op") in self.ONCE:
                raise
        try:
            fresh = next((f for f in self._frames(s) if f["frame_id"] == frame["frame_id"]), None)
            if fresh is None or (op.get("op") in self.ELEMENT_OPS and fresh["loader"] != frame["loader"]):
                raise CdpProtocolError("document changed")
            frame.update(loader=fresh["loader"], url=fresh["url"], origin=fresh["origin"])
            return self._eval(s, frame, op)
        except CdpProtocolError:
            s.worlds.pop(frame["frame_id"], None)
            if quiet:
                return None
            raise Refused(PAGE_CHANGED) from None

    # --- snapshot ------------------------------------------------------------------------

    @staticmethod
    def _m(s: Session, text: Any) -> str:
        """As a reply says it (handle() does the same to everything of the page in it)."""
        return s.cut({"text": str(text or "")})["text"]

    def snapshot(self, s: Session) -> dict:
        frames = self._frames(s)
        top = frames[0]
        for x in s.code:  # used up; a few digits are cut out no longer than they have to be
            gone = x[1] == top["loader"] and self.clock() - x[3] > 60 and (
                self._run(s, top, {"op": "inspect", "n": x[2]}, quiet=True) or {}).get("connected") is False
            if x[1] != top["loader"] or gone:
                s.code = [y for y in s.code if y is not x]
        snap: dict[str, Any] = {"url": self._m(s, top["url"])[:500], "origin": top["origin"], "title": "", "text": "",
                                "frames": [], "elements": [], "readonly": s.readonly}
        frame_urls = []
        texts = []
        checked_labels: list[str] = []
        for f in frames:
            snap["frames"].append({"id": f["name"], "origin": f["origin"], "url": self._m(s, f["url"])[:200]})
            frame_urls.append(f["url"])
            if f["origin"] == "opaque" and f is not top:
                continue  # never actionable
            data = self._run(s, f, {"op": "enumerate", "main": f is top}, quiet=f is not top)
            if not data:
                continue
            if f is top:
                snap["title"] = br.clean(str(data.get("title") or ""))[:300]
                snap["text"] = br.MARKERS.sub("", br.clean(str(data.get("text") or "")))[:MAX_TEXT]
            else:
                texts.append(str(data.get("text") or ""))
            if f["origin"] == "opaque":
                continue
            checked_labels += [br.MARKERS.sub("", br.clean(str(x)))[:120] for x in (data.get("checked") or [])[:100]
                               if isinstance(x, str)]
            for e in (data.get("elements") or [])[:MAX_ELEMENTS]:
                if not isinstance(e, dict) or not isinstance(e.get("n"), int) or e.get("kind") not in (
                        "input", "textarea", "select", "button", "link", "checkbox", "radio"):
                    continue
                item = {"ref": f"{f['name']}:{e['n']}", "frame": f["name"], "frame_origin": f["origin"],
                        "kind": e["kind"]}
                for key, limit in (("type", 30), ("autocomplete", 60), ("name", 80), ("id", 80), ("label", 120)):
                    item[key] = br.MARKERS.sub("", br.clean(str(e.get(key) or "")))[:limit]
                item["required"] = bool(e.get("required"))
                item["checked"] = e.get("checked") if isinstance(e.get("checked"), bool) else None
                item["filled"] = bool(e.get("filled"))
                item["disabled"] = bool(e.get("disabled"))
                if e["kind"] == "select":
                    item["options"] = [br.MARKERS.sub("", br.clean(str(o)))[:60] for o in (e.get("options") or [])[:30]]
                    item["selected"] = br.clean(str(e.get("selected") or ""))[:60] or None
                snap["elements"].append(item)
        says = [login_caption(el["label"]) for el in snap["elements"] if el["kind"] in ("button", "link")]
        if any(LOGOUT.fullmatch(x) for x in says) and not any(re.fullmatch(r"(log|sign) ?in|kirjaudu( sisään)?|logga in|anmelden|einloggen|войти", x, re.I) for x in says):
            s.seen_account = True  # signed in, however that came about
        snap["totals"] = extract_totals(snap["text"])
        snap["gate_totals"] = snap["totals"] + extract_totals(snap["text"], GATE_WORDS)
        snap["priced"] = bool(PRICE.search(snap["text"]) or any(PRICE.search(t) for t in texts))
        # Every checked box counts, also hidden ones and role=switch toggles (they're sent with the form too).
        labels = [el["label"] for el in snap["elements"] if el["kind"] == "checkbox" and el["checked"]] + checked_labels
        snap["risky_checked"] = list(dict.fromkeys(x for x in labels if RISKY.search(x or "")))
        snap["captcha"] = any(CAPTCHA_FRAME.search(u) for u in frame_urls) or br.looks_like_bot_check(
            snap["text"], snap["title"])
        if snap["captcha"] and s.login_used and s.chain == s.gen:  # ("Please verify it's you" is what a short page that asks for a code says)
            snap["captcha"] = any(CAPTCHA_FRAME.search(u) for u in frame_urls) or bool(
                br.BOT_STRONG.search(snap["text"]) or br.BOT_TITLE.search(snap["title"]))
        snap["threeds"] = any(THREEDS_FRAME.search(urllib.parse.urlsplit(u).netloc + urllib.parse.urlsplit(u).path)
                              for u in frame_urls[1:]) or bool(THREEDS_TEXT.search(snap["text"] + " ".join(texts)))
        return snap

    def _reply(self, s: Session) -> dict:
        return {"ok": True, "session": s.id, "snapshot": self.snapshot(s)}

    def snapshot_cmd(self, req: dict) -> dict:
        return self._reply(self._session(req, writable=False))

    # --- elements --------------------------------------------------------------------------

    def _element(self, s: Session, ref: Any, frames: list[dict] | None = None) -> tuple[dict, dict, int]:
        """(frame, current description, number) for a ref like 'f1:12'; refuses anything not actionable."""
        if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
            raise Refused("unknown element")
        name, num = ref.split(":")
        fid = s.frame_id(name)
        frames = frames or self._frames(s)
        frame = next((f for f in frames if f["frame_id"] == fid), None)
        if frame is None or frame["origin"] == "opaque":
            raise Refused("that element is gone (the page changed); take a new snapshot")
        cached = s.worlds.get(frame["frame_id"])
        if not cached or cached[0] != frame["loader"]:
            raise Refused("that element is gone (the page changed); take a new snapshot")
        d = self._run(s, frame, {"op": "inspect", "n": int(num)}) or {}
        if not d.get("connected"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return frame, d, int(num)

    def _after_action(self, s: Session) -> dict:
        self._settle(s, 8)
        return self._reply(s)

    def click(self, req: dict) -> dict:
        s = self._session(req)
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") not in ("button", "link", "checkbox", "radio", "input"):
            raise Refused("that element can't be clicked")
        if d.get("disabled"):
            raise Refused("that element is disabled right now (the page may want something filled first)")
        live = s.chain is not None and s.chain == s.gen - 1
        if s.secrets or live:
            g = self._gate(s, frame, n)
            if g["leaks"]:
                raise Refused("that would put the login that was filled in into an address")
            if live and frame["parent"] is None and s.steps < CODE_STEPS and (self._harmless_step(s, d, g) or self._leads_to_login(s, frame, d, g)):
                s.chain, s.steps = s.gen, s.steps + 1
        self._acted = True
        r = self._run(s, frame, {"op": "click", "n": n}) or {}
        if not r.get("ok"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    def type_text(self, req: dict) -> dict:
        s = self._session(req)
        text = req.get("text")
        if not isinstance(text, str) or len(text) > 500 or any(ord(c) < 32 and c not in "\n" for c in text):
            raise Refused("text must be up to 500 ordinary characters")
        if has_card_number(text):
            raise Refused("that looks like a card number; card details are filled only with the owner's approval")
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") not in ("input", "textarea") or (d.get("kind") == "input" and d.get("type") not in TEXT_TYPES):
            raise Refused("that element isn't a text field")
        if d.get("disabled"):
            raise Refused("that field is disabled right now")
        if any(SECRET_FIELD.search(str(d.get(k) or "")) for k in ("type", "autocomplete", "name", "id")):
            raise Refused("that field is for card, password or code details; those are only filled with approval")
        self._not_for_a_code(s, frame, n, d, text)
        self._part_of_a_form(s, frame, n, d)
        ident = re.sub(r"[_\-.\[\]]+", " ", " ".join(str(d.get(k) or "") for k in ("name", "label", "id")))
        if d.get("type") != "search" and not (d.get("type") == "number" and QUANTITY.search(ident)):
            s.filled = True  # something was entered: buttons may send it
        s.typed.add(frame["loader"])
        self._acted = True
        r = self._run(s, frame, {"op": "set", "n": n, "value": text}) or {}
        if not r.get("ok"):
            raise Refused("that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    def _not_for_a_code(self, s: Session, frame: dict, n: int, d: dict, text: str) -> None:
        """Ampi reads the owner's email, where codes arrive: it types none, however the field is called. (What looks like one, into a
        field that isn't plainly for something else, where the page asks for one.)"""
        no = Refused("that field is for a one-time code: only the owner enters one, on the phone (`signin code`, right after `signin fill`)")
        if code_field(d, loose=s.account):
            raise no
        bare = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))
        like_one = 0 < len(bare) <= 10 and any(c.isdigit() for c in bare) or re.fullmatch(r"[A-Z]{4,10}", bare)
        if like_one and d.get("kind") == "input" and d.get("type") in CODE_TYPES and not any(
                PLAIN_FIELD.fullmatch(x := _spaced(d, k).strip(" *:")) or QUANTITY.fullmatch(x) for k in ("name", "id", "label")):
            r = self._run(s, frame, {"op": "code", "n": n}) or {}
            if CODE_SENT.search(OTHER_CODE.sub(" ", br.clean(str(r.get("words") or "")))) and not NOT_CODE.search(_spaced(d, "label") if d.get("label") else _spaced(d, "name", "id")):
                raise no

    def _part_of_a_form(self, s: Session, frame: dict, n: int, d: dict) -> None:
        """In an account a field that isn't sent with a button can act as soon as it changes (a plan, a setting)."""
        if s.account and d.get("type") != "search":
            g = self._gate(s, frame, n)
            if not g["form_button"] and not g["search"]:
                raise Refused("in a signed-in account a field that isn't in a form with a button may act at once: use "
                              "`checkout submit REF --option … --what …`, which asks the owner")

    def _settable(self, s: Session, frame: dict, n: int, d: dict, option: Any) -> None:
        """A list or a text field, set after the owner's approval (bind_submit/submit with `option`)."""
        if not isinstance(option, str) or not 0 < len(option) <= 200 or any(ord(c) < 32 for c in option) or has_card_number(option):
            raise Refused("give the option's text (or the text for the field), up to 200 ordinary characters")
        if d.get("disabled"):
            raise Refused("that field is disabled right now")
        if d.get("kind") == "select":
            return
        if d.get("kind") not in ("input", "textarea") or (d.get("kind") == "input" and d.get("type") not in TEXT_TYPES):
            raise Refused("only a list or a text field can be set")
        if any(SECRET_FIELD.search(str(d.get(k) or "")) for k in ("type", "autocomplete", "name", "id")):
            raise Refused("that field is for card, password or code details")
        self._not_for_a_code(s, frame, n, d, option)

    def choose(self, req: dict) -> dict:
        s = self._session(req)
        option = req.get("option")
        if not isinstance(option, str) or not 0 < len(option) <= 200:
            raise Refused("give the option's text")
        frame, d, n = self._element(s, req.get("ref"))
        if d.get("kind") != "select":
            raise Refused("that element isn't a list to choose from")
        self._part_of_a_form(s, frame, n, d)
        self._acted = True
        r = self._run(s, frame, {"op": "choose", "n": n, "option": option}) or {}
        if not r.get("ok"):
            raise Refused("no such option in that list" if r.get("why") == "option" else
                          "that element is gone (the page changed); take a new snapshot")
        return self._after_action(s)

    # --- approvals: bind, fill, pay ------------------------------------------------------

    BOUND = ("tag", "type", "autocomplete", "name", "id")

    def bind(self, req: dict) -> dict:
        s = self._session(req)
        refs = req.get("refs")
        if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
            raise Refused("give 1-12 elements")
        frames = self._frames(s)
        out = []
        for ref in refs:
            frame, d, _ = self._element(s, ref, frames)
            item = {"ref": ref, "frame": frame["name"], "frame_id": frame["frame_id"], "frame_origin": frame["origin"],
                    "main": frame is frames[0], "kind": d.get("kind"), "disabled": bool(d.get("disabled"))}
            for k in self.BOUND + ("label",):
                item[k] = str(d.get(k) or "")[:120]
            item["role"] = str(d.get("role") or "")[:30]
            out.append(item)
        snap = self.snapshot(s)
        return {"ok": True, "session": s.id, "origin": snap["origin"], "bindings": out, "totals": snap["totals"],
                "risky_checked": snap["risky_checked"]}

    # --- submit: a button that commits to something, after approval ---------------------------

    def _form(self, s: Session, frames: list[dict], bframe: dict) -> tuple[list[dict], str]:
        """(what is filled in on the page and in the button's frame, a fingerprint that also covers hidden fields).
        Anything that can't be read or shown in full is refused, never left out."""
        fields: list[dict] = []
        raw: list = []
        for f in ([frames[0]] if bframe["frame_id"] == frames[0]["frame_id"] else [frames[0], bframe]):
            data = self._run(s, f, {"op": "form"})
            if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("hidden"), list):
                raise Refused("the form on this page can't be read; the owner has to do this")
            if data.get("problem"):
                raise Refused(f"the page has {str(data['problem'])[:60]}, which can't be shown to the owner; they have to do this")
            raw.append([f["frame_id"], data["fields"], data["hidden"]])
            for e in data["fields"]:
                value = e.get("value")
                shown = True if value is True else br.MARKERS.sub("", br.clean(str(value or "")))
                if isinstance(shown, str) and has_card_number(shown):
                    shown = "(secret)"
                fields.append({"label": br.MARKERS.sub("", br.clean(str(e.get("label") or "")))[:120],
                               "kind": str(e.get("kind") or "")[:20], "value": shown, "shown": bool(e.get("shown"))})
        return fields, hmac.new(s.salt, json.dumps(raw, sort_keys=True).encode(), "sha256").hexdigest()

    def _gate(self, s: Session, frame: dict, n: int) -> dict:
        g = self._run(s, frame, {"op": "gate", "n": n}) or {}
        if not g.get("connected"):
            raise Refused(PAGE_CHANGED)
        return {"tag": str(g.get("tag") or "")[:20], "texts": [br.MARKERS.sub("", br.clean(str(t)))[:200] for t in (g.get("texts") or [])[:12]],
                "ident": br.clean(str(g.get("ident") or ""))[:120], "submits": bool(g.get("submits")), "search": bool(g.get("search")),
                "plain": bool(g.get("plain")), "form_button": bool(g.get("formButton")), "dialog": str(g.get("dialog") or "")[:10],
                "leaks": bool(g.get("leaks")),
                "dest": br.clean(str(g.get("dest") or ""))[:1000], "method": str(g.get("method") or "")[:10],
                "href": br.clean(str(g.get("href") or ""))[:1000],
                "around": br.MARKERS.sub("", br.clean(str(g.get("around") or "")))[:600]}

    def gate(self, req: dict) -> dict:
        """What pressing an element would really press, so the daemon can decide whether it needs the owner."""
        s = self._session(req)
        frame, d, n = self._element(s, req.get("ref"))
        g = self._gate(s, frame, n)
        snap = self.snapshot(s)
        return {"ok": True, "session": s.id, "kind": d.get("kind"), "frame_origin": frame["origin"], "filled": s.filled,
                "totals": snap["gate_totals"], "priced": snap["priced"],
                "frames": sorted({f["origin"] for f in self._frames(s)}), "signed_in": s.account, "type": d.get("type"), "label": str(d.get("label") or "")[:120],
                "role": str(d.get("role") or "")[:30], **g}

    @staticmethod
    def _seen(s: Session, g: dict, title: str) -> str:
        """A fingerprint of the text the owner is shown next to the button. A running countdown there refuses the press:
        telling it from a changed time isn't safe."""
        return hmac.new(s.salt, re.sub(r"\s+", " ", f"{title}\n{g['around']}").encode(), "sha256").hexdigest()

    @staticmethod
    def _press(g: dict) -> dict:
        return {k: g[k] for k in ("tag", "texts", "dest", "method", "submits")}

    def bind_submit(self, req: dict) -> dict:
        b = self.bind({**req, "refs": [req.get("ref")]})
        s = self._session(req)
        frames = self._frames(s)
        bframe, d, n = self._element(s, req.get("ref"), frames)
        box, option = d.get("kind") in ("checkbox", "radio"), req.get("option")
        if option is not None:
            if not s.account:
                raise Refused("use `checkout choose` or `checkout type` for that")
            self._settable(s, bframe, n, d, option)
        elif (d.get("kind") not in ("button", "link") and not box) or d.get("disabled"):
            raise Refused("give the ref of an enabled button")
        if box and not d.get("checked") and RISKY.search(str(d.get("label") or "")):
            raise Refused("that box may add a subscription or cost; the owner ticks it")
        snap = self.snapshot(s)
        g = self._gate(s, bframe, n)
        fields, digest = self._form(s, frames, bframe)
        return {"ok": True, "session": s.id, "origin": b["origin"], "url": frames[0]["url"], "button": b["bindings"][0],
                "totals": snap["totals"], "risky_checked": snap["risky_checked"], "captcha": snap["captcha"],
                "title": snap["title"], "form": fields, "digest": digest, "gen": s.gen, "press": self._press(g),
                "seen": self._seen(s, g, snap["title"]), "priced": snap["priced"], "gate_totals": snap["gate_totals"],
                "around": g["around"], "frames": sorted({f["origin"] for f in frames}),
                **({"set": d["kind"], "option": option, "was": str(d.get("selected") or "")[:60]} if option is not None else
                   {"box": d["kind"], "checked": bool(d.get("checked"))} if box else {})}

    def submit(self, req: dict) -> dict:
        s = self._session(req)
        self._phase = "verify"
        changed = Refused("the page changed since the approval; nothing was pressed")
        b = req.get("button")
        if not isinstance(b, dict):
            raise Refused("bad button")
        frames = self._frames(s)
        if frames[0]["origin"] != req.get("origin") or frames[0]["origin"] == "opaque" \
                or self._m(s, frames[0]["url"]) != req.get("url"):
            raise changed
        try:
            bframe, d, n = self._element(s, b.get("ref"), frames)
        except Refused:
            raise changed from None
        if (bframe["frame_id"] != b.get("frame_id") or bframe["origin"] != b.get("frame_origin")
                or d.get("kind") not in (("button", "link", "checkbox", "radio") if req.get("option") is None else (req.get("set"),))
                or d.get("disabled") or d["kind"] in ("checkbox", "radio") and (d["kind"], bool(d.get("checked"))) != (req.get("box"), req.get("checked"))
                or any(self._m(s, str(d.get(k) or "")[:120]) != str(b.get(k) or "") for k in self.BOUND + ("label",))):
            raise changed
        if req.get("gen") != s.gen:
            raise Refused("something else was done in the browser since the approval; nothing was pressed")
        op: dict = {"op": "click", "n": n}
        if req.get("option") is not None:
            self._settable(s, bframe, n, d, req["option"])
            op = {"op": "choose", "n": n, "option": req["option"]} if d["kind"] == "select" else {"op": "set", "n": n, "value": req["option"]}
        g = self._gate(s, bframe, n)
        if s.cut(self._press(g)) != req.get("press"):
            raise changed
        if g["leaks"]:
            raise Refused("that would put the login that was filled in into an address; nothing was pressed")
        snap = self.snapshot(s)
        if self._seen(s, g, snap["title"]) != req.get("seen"):
            raise changed
        if totals_key(snap["totals"]) != totals_key(req.get("expect_totals") or []) or snap["captcha"] \
                or snap["risky_checked"] or sorted({f["origin"] for f in frames}) != req.get("frames"):
            raise changed
        if self._form(s, frames, bframe)[1] != req.get("digest"):
            raise changed
        carries = op["op"] == "click" and self._carries(s, frames, bframe, d, n, g, snap)
        # Whatever happens from here on, the press may have happened: never a definite refusal.
        s.gen += 1  # an approval is good for one press
        if carries:
            s.chain, s.steps = s.gen, s.steps + 1
        self._phase = "sent"
        try:
            if op["op"] != "click":
                s.filled = True
                s.typed.add(bframe["loader"])
            r = self._run(s, bframe, op) or {}
            if r.get("why") == "option":
                self._phase = "verify"
                return self._error("no such option in that list; nothing was changed")
            if not r.get("ok"):
                return self._error("the button vanished while being pressed; it MAY have gone through")
            return {**self._after_action(s), "phase": "sent"}
        except (Refused, CdpProtocolError):
            return self._error("after pressing, the page changed or went somewhere this browser doesn't open; it MAY "
                               "have gone through")
        except Exception:
            self.retire = True
            self.drop()
            return self._error("the browser failed after pressing; it MAY have gone through")

    def _verify(self, s: Session, req: dict, fields: Any, allow_card: bool) -> tuple[list[dict], list[tuple]]:
        """All-or-nothing: every field must still be exactly the element that was approved."""
        changed = Refused("the page changed since the approval; nothing was filled")
        if not isinstance(fields, list) or not 1 <= len(fields) <= 12:
            raise Refused("bad fields")
        frames = self._frames(s)
        if frames[0]["origin"] != req.get("origin") or frames[0]["origin"] == "opaque":
            raise changed
        psp = {o for o in req.get("psp_origins") or [] if isinstance(o, str)}
        plan = []
        for f in fields:
            if not isinstance(f, dict) or not isinstance(f.get("binding"), dict) or not isinstance(f.get("value"), str):
                raise Refused("bad fields")
            b = f["binding"]
            if f.get("ref") != b.get("ref"):
                raise changed
            try:
                frame, d, n = self._element(s, b.get("ref"), frames)
            except Refused:
                raise changed from None
            if frame["frame_id"] != b.get("frame_id") or frame["origin"] != b.get("frame_origin"):
                raise changed
            if any(self._m(s, str(d.get(k) or "")[:120]) != str(b.get(k) or "") for k in self.BOUND):
                raise changed
            if d.get("kind") not in ("input", "textarea"):
                raise changed
            if not f.get("card") and code_field(d, loose=s.account):
                raise Refused("that field is for a one-time code: only the owner enters one, on the phone (`signin code`)")
            if f.get("card"):
                if not allow_card:
                    raise Refused("card details are only filled as part of a payment")
                in_psp_frame = frame is not frames[0] and frame["origin"] in psp
                hosted = frame is frames[0] and frames[0]["origin"] in psp
                if not (in_psp_frame or hosted):
                    raise Refused("card details go only into a payment provider's field; this one isn't")
            plan.append((frame, n, f["value"]))
        return frames, plan

    def fill(self, req: dict) -> dict:
        s = self._session(req)
        self._phase = "verify"
        _, plan = self._verify(s, req, req.get("fields"), allow_card=False)
        self._phase = "filled"
        self._acted = s.filled = True
        s.typed |= {frame["loader"] for frame, _, _ in plan}
        self._set_all(s, plan)
        self._phase = None  # done: later problems (settling, the snapshot) are ordinary errors
        return self._after_action(s)

    # --- signing in (only ampi-signin) ---------------------------------------------------------

    LOGIN_FACTS = ("hasForm", "sameForm", "passwords", "extra", "boxes", "shown", "method", "action", "boxed")

    def _login(self, s: Session, req: dict) -> tuple[dict, dict, dict, dict | None]:
        """(top frame, {"user"/"password"/"press": (description, number)}, facts of the form, what the press would press): a
        form that signs in, or a refusal. What it is comes from the page, never from the caller. Where it sends the login is
        in the facts ("targets"): ampi-signin compares them with the sites the login is for."""
        if s.signed_in:
            raise Refused("a login was filled in here already; close the session to sign in again")
        if s.full:
            raise Refused("this session was on too many sites; open the sign-in page in a new one")
        frames = self._frames(s)
        top = frames[0]
        host = login_host(top["url"])
        if not host or top["origin"] != f"https://{urllib.parse.urlsplit(top['url']).netloc.lower().removesuffix(':443')}":
            raise Refused("this isn't an https page a login can be used on")
        if top["loader"] in s.typed:
            raise Refused("something was typed into this page; open it again and leave the filling to the sign-in")
        els: dict[str, tuple[dict, int]] = {}
        for k in ("user", "password", "code", "press"):
            if req.get(k) is None:
                continue
            frame, d, n = self._element(s, req[k], frames)
            if frame is not top:
                raise Refused("the sign-in form is inside a frame; that isn't supported")
            if d.get("disabled") and k != "press":  # (the button often wakes up when the fields are filled)
                raise Refused(f"the {k} field is disabled")
            els[k] = (d, n)
        if not {"user", "password"} & set(els):
            raise Refused("give the user field, the password field, or both")
        if "code" in els:
            if "password" not in els:
                raise Refused("a code next to a user name alone isn't supported")
            cd, cn = els["code"]
            self._code_element(cd)
            if cd.get("max") == 1:
                raise Refused("a code in several boxes next to a password isn't supported")
            if "new-password" in str(cd.get("autocomplete") or "").lower() or cd.get("type") == "password" and not code_field(cd):
                raise Refused("that doesn't look like the field for a one-time code")
            self._code_in_time(s)
            r = self._run(s, top, {"op": "code", "n": cn}) or {}
            self._code_place_ok(s, frames, self.snapshot(s), cd, r)
            if CODE_SIGNUP.search(br.clean(" \n ".join([str(r.get("words") or ""), *map(str, r.get("captions") or []), address_words(top["url"])]))):
                raise Refused("that form seems to be for something else than signing in; the owner does that")
        # A lone password field is how sites ask "is it really you?" before something important. It is a sign-in only as the
        # second page of one.
        if "user" not in els and not s.user_step:
            raise Refused("a password field alone isn't a sign-in (it may confirm something in an account); give the user field too")
        if "password" not in els and s.user_step:
            raise Refused("the user name was filled in already; now the password")
        if "password" in els:
            d = els["password"][0]
            if d.get("tag") != "input" or d.get("type") != "password":
                raise Refused("that isn't a password field")
            if "new-password" in str(d.get("autocomplete") or "").lower():
                raise Refused("that field sets a new password; only the owner does that")
        if "user" in els:
            d = els["user"][0]
            words = " ".join(str(d.get(k) or "") for k in ("autocomplete", "name", "id", "label"))
            if d.get("tag") != "input" or d.get("type") not in ("", "text", "email", "tel"):
                raise Refused("that isn't a field for a user name")
            if d.get("type") not in ("email", "tel") and not USER_FIELD.search(words):
                raise Refused("that doesn't look like the field for a user name or email")
        facts = self._run(s, top, {"op": "login", "user": els["user"][1] if "user" in els else None,
                                   "secret": els["password"][1] if "password" in els else None,
                                   **({"code": els["code"][1]} if "code" in els else {})}) or {}
        if not facts.get("connected"):
            raise Refused(PAGE_CHANGED)
        facts = {k: facts.get(k) for k in self.LOGIN_FACTS}
        if not facts["sameForm"]:
            raise Refused("those fields don't belong to the same form")
        if facts["boxed"] is not False:
            raise Refused("that page shows content of others inside it (a sandboxed frame); signing in there is for the owner")
        if not facts["shown"]:
            raise Refused("one of those fields isn't visible on the page")
        if "password" in els and facts["passwords"] != 1:
            raise Refused("that password field isn't usable here" if not facts["passwords"] else
                          "that form has several password fields: it registers or changes a password, it doesn't sign in")
        if facts["extra"] or not isinstance(facts["boxes"], int) or facts["boxes"] > 2:
            raise Refused("that form asks for more than a sign-in does (other fields); the owner does that")
        if facts["method"] not in ("", "post"):
            raise Refused("that form would put the login into the address of a page; refusing")
        press = self._login_press(s, frames, els) if "press" in els else None
        facts["targets"] = sorted({host, *(login_host(u) for u in (facts["action"], press["to"] if press else "") if u)})
        if "" in facts["targets"]:
            raise Refused("that form sends the login to something that isn't an https site; refusing")
        return top, els, facts, press

    def _login_press(self, s: Session, frames: list[dict], els: dict) -> dict:
        """The sign-in button: it submits the form of the fields, to the same site, says so and nothing else, on a page with
        nothing to pay. Anything else is for `checkout submit`, where the owner sees what is pressed."""
        instead = "; fill in without --press and use `checkout submit` for the button, which asks the owner"
        d, n = els["press"]
        field = next(els[k][1] for k in ("password", "user") if k in els)
        if d.get("kind") != "button":
            raise Refused("that isn't a button" + instead)
        g = self._run(s, frames[0], {"op": "gate", "n": n, "with": field}) or {}
        if not g.get("connected") or g.get("tag") not in ("button", "input") or not g.get("submits") or not g.get("withForm"):
            raise Refused("that isn't the button of that form" + instead)
        texts = [login_caption(t) for t in g.get("texts") or []]
        if not texts or not all(LOGIN_CAPTION.fullmatch(t) for t in texts):
            raise Refused("that button doesn't say it signs in" + instead)
        if str(g.get("method") or "") not in ("", "post", "get" if not g.get("ownMethod") else "post"):
            raise Refused("that button would put the login into the address of a page; refusing")
        snap = self.snapshot(s)
        if snap["gate_totals"] or snap["priced"] or snap["risky_checked"] or any(PAY_HOST.search(f["origin"]) for f in frames):
            raise Refused("there are prices or a payment form on this page" + instead)
        return {k: g.get(k) for k in ("tag", "texts", "to", "submits", "ident")}

    def bind_login(self, req: dict) -> dict:
        """Which site the form is on (the owner is asked for exactly that one), which sites this session was on, and what
        the form is, to check it again."""
        s = self._session(req)
        top, els, facts, press = self._login(s, req)
        snap = self.snapshot(s)
        if snap["captcha"]:
            raise Refused("the page has a captcha; signing in there is for the owner")
        return {"ok": True, "session": s.id, "host": login_host(top["url"]), "visited": sorted(s.visited),
                "targets": facts["targets"], "bound": {
            "gen": s.gen, "frame_id": top["frame_id"], "origin": top["origin"], "loader": top["loader"], "facts": facts, "press": press,
            "elements": {k: {"ref": req[k], **{b: str(d.get(b) or "")[:120] for b in self.BOUND}} for k, (d, _) in els.items()}}}

    def fill_login(self, req: dict) -> dict:
        """Fills in what the owner unlocked, into exactly what was bound, and presses the button if one was given."""
        s = self._session(req)
        self._phase = "verify"
        changed = Refused("the page changed meanwhile; nothing was filled in")
        bound = req.get("bound")
        if not isinstance(bound, dict) or not isinstance(bound.get("elements"), dict):
            raise Refused("bad request")
        if bound.get("gen") != s.gen:
            raise changed
        try:
            top, els, facts, press = self._login(s, {k: v.get("ref") for k, v in bound["elements"].items() if isinstance(v, dict)})
        except Refused:
            raise changed from None
        if (top["frame_id"] != bound.get("frame_id") or top["origin"] != bound.get("origin")
                or top["loader"] != bound.get("loader") or facts != bound.get("facts") or press != bound.get("press")
                or login_host(top["url"]) != req.get("host")
                or {k: {"ref": bound["elements"][k].get("ref"), **{b: str(d.get(b) or "")[:120] for b in self.BOUND}}
                    for k, (d, _) in els.items()} != bound["elements"]):
            raise changed
        values = {k: req.get(k) for k in ("user", "password", "code") if k in els}
        if any(not fillable(v) for v in values.values()) or not fillable(req.get("name")) or "code" in values and not a_code(values["code"]):
            raise Refused("bad request")
        if "code" in values:
            values["code"] = self._code_fits(values["code"], els["code"][0].get("max"), els["code"][0].get("type"))
            s.code.append((Code(values["code"]), top["loader"], els["code"][1], self.clock()))
        live = s.chain is not None and s.chain == s.gen
        path = urllib.parse.urlsplit(top["url"])._replace(query="", fragment="").geturl()
        further = CODE_DANGER.search(address_words(path)) or any(CODE_LEADS.search(x) for x in lead_words(top["url"]))
        s.gen += 1
        s.chain = s.gen if live and not s.full and not further else None
        if s.chain is None and not s.broke:
            s.broke = "the sign-in page's own address leads to something else than the site's start"
        field = next(els[k] for k in ("password", "user") if k in els)
        s.chain_at, s.steps, s.step_form = self.clock(), 0, (top["loader"], field[1], self._what(field[0]))
        if "code" in els:
            s.codes, s.code_place = s.codes + 1, self._place(top, els["code"][0], facts.get("action"))
        s.filled, before = True, (s.signed_in, s.user_step)
        s.secrets += [Name(req["name"]), *([values["password"]] if "password" in values else [])]
        if "password" in values:
            s.signed_in = True
        else:
            s.user_step = True
        self._phase = "filled"
        # Checked and set in one go: page scripts run while the first field is set, and nothing else gets in between.
        r = self._run(s, top, {"op": "signin", "user": els["user"][1] if "user" in els else None,
                               "secret": els["password"][1] if "password" in els else None, "values": values,
                               **({"code": els["code"][1]} if "code" in els else {}),
                               "expect": {k: v for k, v in facts.items() if k != "targets"}}) or {}
        if not r.get("ok"):
            if r.get("set") is False:  # nothing was put in yet
                s.signed_in, s.user_step = before
                s.chain = s.gen if live else None
                if "code" in values:
                    s.code, s.codes = s.code[:-1], s.codes - 1
                self._phase = "verify"
                raise changed
            raise Refused("the page changed while it was being filled in; close the session and start again")
        if press is None:
            return {"ok": True, "session": s.id, "pressed": False}
        for _ in range(6):  # forms enable their button when the fields are filled, some after a moment
            self.sleep(0.5)
            cur = self._run(s, top, {"op": "inspect", "n": els["press"][1]}) or {}
            if not cur.get("connected") or not cur.get("disabled"):
                break
        if not cur.get("connected") or cur.get("disabled"):
            raise Refused("the login was filled in, but the sign-in button is gone or disabled; it was not pressed")
        self._phase = "sent"
        try:
            r = self._run(s, top, {"op": "clickif", "n": els["press"][1], "expect": press}) or {}
            if not r.get("ok"):
                now = self._frames(s)[0]
                if now["loader"] != top["loader"] or now["url"] != top["url"]:
                    raise Refused(PAGE_CHANGED)
                return {"ok": True, "session": s.id, "pressed": False}
            return {"ok": True, "session": s.id, "pressed": True, "moved": self._after_signin(s, top, els)}
        except (Refused, CdpProtocolError):
            return self._error("after the sign-in button was pressed, the page changed or went somewhere this browser doesn't "
                               "open; look at it before trying again")

    def _after_signin(self, s: Session, top: dict, els: dict) -> bool:
        """Waits until the page reacts (another address or document, or the field is gone); False when it hasn't in time."""
        start = self.clock()
        while self.clock() - start < SIGNIN_SETTLE:
            self.sleep(0.5)
            now = self._frames(s)[0]
            if now["url"] != top["url"] or now["loader"] != top["loader"]:
                self._settle(s, NAV_TIMEOUT)
                return True
            d = self._run(s, now, {"op": "inspect", "n": els.get("password", els.get("user"))[1]}, quiet=True) or {}
            if not d.get("connected") or not d.get("visible", True):
                self._settle(s, NAV_TIMEOUT)
                return True
        self._check_top(s)
        return False

    # --- the one-time code of a sign-in (only ampi-signin) ---------------------------------------

    CODE_FACTS = ("hasForm", "count", "max", "type", "extra", "boxes", "shown", "method", "action", "boxed", "press")

    @staticmethod
    def _harmless_step(s: Session, d: dict, g: dict) -> bool:
        """A plain click that may come between a sign-in and its code: a cookie banner, or which way to get the code."""
        texts = [t for t in (login_caption(t) for t in g["texts"]) if t]
        if g["dialog"] == "cookies" and g["tag"] in ("button", "input") and not g["submits"] and texts and all(COOKIE_CAPTION.fullmatch(t) for t in texts):
            return True
        label = str(d.get("label") or "")
        return s.login_used and d.get("kind") == "radio" and g["tag"] == "input" and bool(CODE_WAY.search(label)) and not CODE_DANGER.search(label)

    @staticmethod
    def _leads_to_login(s: Session, frame: dict, d: dict, g: dict) -> bool:
        """Before the sign-in: a link to the start or sign-in page, or a button that opens the sign-in form."""
        if s.account:
            return False
        texts = [t for t in (login_caption(t) for t in g["texts"]) if t]
        named = any(LOGIN_NAV.fullmatch(t) for t in texts) and not any(CODE_DANGER.search(t) or NAV_NOT.search(t) for t in texts)
        if d.get("kind") == "link":
            if g["dest"].lower().startswith("javascript:"):
                return named
            elsewhere = g["dest"].split("#")[0] != frame["url"].split("#")[0]
            return same_site(g["dest"], frame["url"]) and plain_entry(g["dest"], asked=named) and (named or g["plain"] and elsewhere)
        return d.get("kind") == "button" and named and not g["submits"]

    @staticmethod
    def _what(d: dict) -> list[str]:
        return [str(d.get(b) or "")[:120] for b in Controller.BOUND]

    @staticmethod
    def _place(top: dict, d: dict, action: Any = "") -> list:
        """Where a code went: the page (with its route and what it was asked for), where its form goes, and the field."""
        u = urllib.parse.urlsplit(top["url"])
        return [u.path, urllib.parse.urlsplit(str(action or "")).path or u.path, sorted({k for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True)}),
                u.fragment.partition("?")[0], *(str(d.get(b) or "")[:120] for b in ("tag", "type", "autocomplete", "name"))]

    @staticmethod
    def _same_place(first: list | None, now: list) -> bool:
        """Another try: on the same page, or on the one the first form went to (which then says that the code was wrong)."""
        new = set(now[2]) ^ set(first[2]) if first is not None else set()
        return first is not None and now[0] in first[:2] and now[3:] == first[3:] and all(PLACE_QUERY.fullmatch(k) for k in new)

    @staticmethod
    def _step_to(g: dict, here: str) -> bool:
        """Does a step stay within the sign-in? The same site, no address inside the address, nothing new asked for in it."""
        dest = str(g["dest"] or "")
        if not dest or dest.lower().startswith("javascript:"):
            return True
        route = lambda u: x if (x := urllib.parse.urlsplit(u).fragment).startswith(("/", "!")) else ""  # noqa: E731
        if dest.split("#")[0] == here.split("#")[0] and route(dest) in ("", route(here)):
            return True
        keys = lambda u: {k.lower() for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query, keep_blank_values=True)}  # noqa: E731
        away = lambda u: {x.lower() for x in STEP_AWAY.findall(address_words(u))}  # noqa: E731
        return (login_host(dest) == login_host(here) != "" and not nested(dest) and not CODE_DANGER.search(address_words(dest))
                and away(dest) <= away(here) and keys(dest) <= keys(here) | STEP_QUERY)

    def _carries(self, s: Session, frames: list[dict], bframe: dict, d: dict, n: int, g: dict, snap: dict) -> bool:
        """Does this press (the owner approved it) belong to a sign-in, so that its code may still be filled in after it?"""
        if s.chain is None or s.chain != s.gen or s.steps >= CODE_STEPS or bframe is not frames[0]:
            return False
        if self._to_pay(snap, frames):
            return False
        if not s.login_used:
            return False
        loader, field, what = s.step_form
        if bframe["loader"] == loader and d.get("kind") == "button":
            r = self._run(s, bframe, {"op": "gate", "n": n, "with": field}) or {}
            cur = self._run(s, bframe, {"op": "inspect", "n": field}) or {}
            says = [t for t in (login_caption(t) for t in g["texts"]) if t]
            known = bool(says) and all(LOGIN_CAPTION.fullmatch(t) or CODE_CAPTION.fullmatch(t) for t in says)
            # The button of the form that was filled in; where there is no form, or the button doesn't send it, one next to the field that says so.
            if cur.get("connected") and self._what(cur) == what and (r.get("submits") and r.get("withForm") or r.get("withBox") and not r.get("submits") and known):
                return True
        if self._harmless_step(s, d, g):
            return not s.codes
        texts = [t for t in (login_caption(t) for t in g["texts"]) if t]
        if d.get("kind") not in ("button", "link"):
            return False
        here = bframe["url"]
        if s.codes:  # a code was filled in: only a new one
            return bool(texts) and all(CODE_AGAIN.fullmatch(t) for t in texts) and self._step_to(g, here)
        if texts and all(CODE_STEP.fullmatch(t) or d["kind"] == "button" and CODE_METHOD.fullmatch(t) for t in texts):
            return self._step_to(g, here)
        return (d["kind"] == "button" and g["submits"] and g["method"] == "post" and bool(texts) and all(CODE_GENERIC.fullmatch(t) for t in texts)
                and self._step_to(g, here))

    @staticmethod
    def _to_pay(snap: dict, frames: list[dict]) -> bool:
        """A total (other than nothing: "Cart 0,00 €"), or a payment frame (not the one Stripe's script puts on every page)."""
        return (any(not re.fullmatch(r"0*(\.0*)?", str(t.get("amount"))) for t in snap["gate_totals"])
                or any(PAY_HOST.search(f["origin"]) and not re.match(r"https://js\.stripe\.com/v3/m-outer", f["url"]) for f in frames))

    def _code_in_time(self, s: Session) -> None:
        if s.chain is None or s.chain != s.gen:
            raise Refused("a code goes in only where a sign-in itself led, and " + (s.broke or "something else was done in this session") + ". Close it, open "
                          "the site's start page in a new session, use its own Log in, and do nothing else before and after the sign-in")
        if s.codes >= CODE_TRIES:
            raise Refused("three codes were tried already; close the session and start again")
        if s.full:
            raise Refused("this session was on too many sites")

    def _code_place_ok(self, s: Session, frames: list[dict], snap: dict, d: dict, r: dict) -> None:
        """Nothing on this page says that a code would confirm something else than a sign-in. (A net, not a wall: the chain is that.)"""
        words = " \n ".join([br.clean(str(r.get("words") or "")), _spaced(d, "name", "id", "label")])
        sends = " \n ".join([*(br.clean(str(x)) for x in (r.get("captions") or [])[:10]), address_words(str(r.get("action") or ""))])
        if self._to_pay(snap, frames) or snap["risky_checked"] or priced(words):
            raise Refused("there is something to pay on this page; the owner does that")
        if (CODE_DANGER.search(words) or CODE_HEAD.search(br.clean(str(r.get("heads") or ""))) or any(
                x.search(y) for x in (CODE_DANGER, CODE_HEAD) for y in (sends, address_words(frames[0]["url"])))
                or any(CODE_FRAME.search(f["url"]) for f in frames[1:])):
            raise Refused("this page seems to ask for a code to confirm something else than a sign-in; the owner does that")

    @staticmethod
    def _code_element(d: dict) -> None:
        if d.get("tag") != "input" or d.get("type") not in CODE_TYPES or not code_field(d, loose=True) \
                or re.search(r"\bcc-|card|cvc|cvv|csc|iban", _spaced(d, "autocomplete", "name", "id", "label"), re.I):
            raise Refused("that doesn't look like the field for a one-time code")

    @staticmethod
    def _code_fits(code: str, length: Any, type_: Any, count: int = 1) -> str:
        plain = code.replace("-", "")
        if count > 1:
            if len(plain) != count:
                raise Refused(f"the code has {len(plain)} characters, the page has {count} boxes; nothing was filled in")
            code = plain
        elif type_ == "number" or isinstance(length, int) and 0 < length < len(code):
            if isinstance(length, int) and 0 < length < len(plain):
                raise Refused(f"the code has {len(plain)} characters, the field takes {length}; nothing was filled in")
            code = plain
        if type_ == "number" and not code.isdigit():
            raise Refused("the field takes digits only; nothing was filled in")
        return code

    def _code(self, s: Session, req: dict) -> tuple[dict, dict, int, dict, dict | None, int, list]:
        """(top frame, the field, its number, facts, what the press would press, the button's number, where it is): a page of
        this sign-in that asks for its code, or a refusal."""
        if not (s.signed_in or s.user_step):
            raise Refused("a code goes in only right after `signin fill`")
        self._code_in_time(s)
        if self.clock() - s.chain_at > CODE_WINDOW:
            raise Refused("the sign-in was too long ago; close the session and start again")
        frames = self._frames(s)
        top = frames[0]
        host = login_host(top["url"])
        if not host or top["origin"] != f"https://{urllib.parse.urlsplit(top['url']).netloc.lower().removesuffix(':443')}":
            raise Refused("this isn't an https page a login can be used on")
        frame, d, n = self._element(s, req.get("code"), frames)
        if frame is not top:
            raise Refused("the code's field is inside a frame; that isn't supported, the owner does that")
        if d.get("disabled"):
            raise Refused("the field for the code is disabled")
        self._code_element(d)
        button = bd = None
        if req.get("press") is not None:
            bframe, bd, button = self._element(s, req["press"], frames)
            if bframe is not top or bd.get("kind") != "button":
                raise Refused("that isn't the button of that form; fill in without --press and use `checkout submit` for it")
        facts = self._run(s, top, {"op": "code", "n": n, "press": button}) or {}
        if not facts.get("connected"):
            raise Refused(PAGE_CHANGED)
        place = self._place(top, d, facts.get("action"))
        if s.codes and not self._same_place(s.code_place, place):
            raise Refused("that isn't where the first code went; the owner does that")
        said = facts
        facts = {k: facts.get(k) for k in self.CODE_FACTS}
        if facts["boxed"] is not False:
            raise Refused("that page shows content of others inside it (a sandboxed frame); signing in there is for the owner")
        if not facts["shown"]:
            raise Refused("that field isn't visible on the page")
        if facts["extra"] or not isinstance(facts["boxes"], int) or facts["boxes"] > 2 or not isinstance(facts["count"], int):
            raise Refused("that form asks for more than a code; the owner does that")
        if facts["method"] not in ("", "post"):
            raise Refused("that form would put the code into the address of a page; refusing")
        snap = self.snapshot(s)
        if snap["captcha"]:
            raise Refused("the page has a captcha; signing in there is for the owner")
        self._code_place_ok(s, frames, snap, d, said)
        press = self._code_press(s, top, button, n, facts) if button is not None else None
        facts["targets"] = sorted({host, *(login_host(u) for u in (facts["action"], press["to"] if press else "") if u)})
        if "" in facts["targets"]:
            raise Refused("that form sends the code to something that isn't an https site; refusing")
        return top, d, n, facts, press, button, place

    def _code_press(self, s: Session, top: dict, button: int, n: int, facts: dict) -> dict:
        instead = "; fill in without --press and use `checkout submit` for the button, which asks the owner"
        g = self._run(s, top, {"op": "gate", "n": button, "with": n}) or {}
        if not g.get("connected") or g.get("tag") not in ("button", "input") or facts["press"] not in ("form", "box"):
            raise Refused("that isn't the button of that form" + instead)
        texts = [login_caption(t) for t in g.get("texts") or []]
        if not texts or not all(LOGIN_CAPTION.fullmatch(t) or CODE_CAPTION.fullmatch(t) for t in texts):
            raise Refused("that button doesn't say it sends the code" + instead)
        if facts["press"] == "form" and str(g.get("method") or "") not in ("", "post", "get" if not g.get("ownMethod") else "post"):
            raise Refused("that button would put the code into the address of a page; refusing")
        return {k: g.get(k) for k in ("tag", "texts", "to", "submits", "ident")}

    def bind_code(self, req: dict) -> dict:
        """Which site asks for a code (the owner is asked for exactly that one), and what the form is, to check it again."""
        s = self._session(req)
        top, d, _, facts, press, _, place = self._code(s, req)
        return {"ok": True, "session": s.id, "host": login_host(top["url"]), "visited": sorted(s.visited), "targets": facts["targets"],
                "bound": {"gen": s.gen, "frame_id": top["frame_id"], "origin": top["origin"], "loader": top["loader"], "facts": facts,
                          "press": press, "place": place, "refs": {k: req[k] for k in ("code", "press") if req.get(k) is not None}}}

    def fill_code(self, req: dict) -> dict:
        """Fills in the code the owner gave, into exactly what was bound, and presses the button if one was given."""
        s = self._session(req)
        self._phase = "verify"
        changed = Refused("the page changed meanwhile; nothing was filled in")
        bound = req.get("bound")
        if not isinstance(bound, dict) or not isinstance(bound.get("refs"), dict) or not a_code(req.get("code")):
            raise Refused("bad request")
        if bound.get("gen") != s.gen:
            raise changed
        try:
            top, d, n, facts, press, button, place = self._code(s, bound["refs"])
        except Refused:
            raise changed from None
        if (top["frame_id"] != bound.get("frame_id") or top["origin"] != bound.get("origin") or top["loader"] != bound.get("loader")
                or facts != bound.get("facts") or press != bound.get("press") or place != bound.get("place")
                or login_host(top["url"]) != req.get("host")):
            raise changed
        code = self._code_fits(req["code"], facts["max"], d.get("type"), facts["count"])
        s.gen += 1
        s.filled = True
        s.code.append((Code(code), top["loader"], n, self.clock()))
        s.chain, s.codes, s.step_form = s.gen, s.codes + 1, (top["loader"], n, self._what(d))
        s.code_place = s.code_place or place
        expect = {k: v for k, v in facts.items() if k != "targets"}
        gone = {"ok": True, "session": s.id, "pressed": False, "moved": True}

        def put(i: int, value: str, **kw: Any) -> dict:
            try:
                return self._run(s, top, {"op": "setcode", "n": n, "i": i, "value": value, "press": button, "expect": expect, **kw}) or {}
            except (Refused, CdpProtocolError):
                return {"ok": False, "set": True}

        look = put(0, "", look=True)  # what an earlier try left in the boxes goes first: a mix of both must never be sent
        for i in sorted(look.get("full") or [], reverse=True) if facts["count"] > 1 else []:
            look = put(i, "") if look.get("ok") else look
        if not look.get("ok"):
            s.codes, s.code = s.codes - 1, s.code[:-1]
            if not s.codes:
                s.code_place = None
            raise changed
        # Many pages send it on the last digit: from the first one on it may have been sent.
        self._phase = "sent"
        for i, part in enumerate(list(code) if facts["count"] > 1 else [code]):
            if not put(i, part).get("ok"):
                return self._error("the page changed while the code was being filled in; look at it before trying again")
        try:
            if self._moved(s, top):
                self._settle(s, NAV_TIMEOUT)
                return gone
            if press is None:
                return {"ok": True, "session": s.id, "pressed": False, "moved": self._after_signin(s, top, {"user": (d, n)})}
            start = self.clock()
            while self.clock() - start < 4:  # a page that sends it by itself mustn't get it twice; and buttons wake up when it is complete
                self.sleep(0.5)
                if self._moved(s, top):
                    self._settle(s, NAV_TIMEOUT)
                    return gone
                f = self._run(s, top, {"op": "inspect", "n": n}, quiet=True) or {}
                if not f.get("connected") or not f.get("visible", True):
                    return {**gone, "moved": True}
            r = self._run(s, top, {"op": "clickif", "n": button, "expect": press, "filled": n}, quiet=True) or {}
            return {"ok": True, "session": s.id, "pressed": bool(r.get("ok")), "moved": self._after_signin(s, top, {"user": (d, n)})}
        except (Refused, CdpProtocolError):
            return self._error("after the code was filled in, the page changed or went somewhere this browser doesn't open; look at it "
                               "before trying again")

    # --- a session the owner gave from their own browser (only ampi-signin) ------------------------

    def _engine_for(self, req: dict) -> str:
        engine = req.get("engine") or "obscura"
        if not isinstance(engine, str) or engine not in ENGINES:
            raise Refused("unknown browser engine")
        if engine not in self.engines:
            raise Refused("the second browser engine isn't installed")
        if self.session is not None:
            raise Refused("another checkout session is open; close it first")
        self._ready_for(engine)
        return engine

    def _ready_for(self, engine: str) -> None:
        if self.retire:  # this engine is on its way out
            raise Refused("the checkout browser is starting afresh; try again in a few seconds")
        # (The first engine idles next to the second one's session, or the other way round.)
        if self.mem_mb() < MIN_MEM_MB + (200 if len(self.engines) > 1 else 0):
            raise Refused("the server is short of memory right now; try again later")

    def bind_session(self, req: dict) -> dict:
        """Before the owner is asked: could a session be opened now?"""
        self._engine_for(req)
        return {"ok": True}

    @staticmethod
    def _cookie(c: Any, host: str) -> dict:
        """A cookie as the engine takes it, or a refusal: one that a browser would send to that host."""
        bad = Refused("that session isn't as expected")
        if not isinstance(c, dict) or any(not isinstance(c.get(k), str) for k in "nvdpx") or any(not isinstance(c.get(k), bool) for k in "osh"):
            raise bad
        d = c["d"]
        if not (COOKIE_NAME.fullmatch(c["n"]) and COOKIE_VALUE.fullmatch(c["v"]) and COOKIE_PATH.fullmatch(c["p"]) and c["x"] in ("", "Lax", "Strict", "None")
                and (d == host or not c["o"] and LOGIN_HOST.fullmatch(d) and host.endswith("." + d))):
            raise bad
        # Never over http, and not for pages of other sites in here (Firefox keeps those apart by site).
        return {"name": c["n"], "value": c["v"], "path": c["p"], "secure": True, "httpOnly": c["h"], "url": f"https://{d}{c['p']}",
                **({} if c["o"] else {"domain": "." + d}), **({"sameSite": {"None": "Lax"}.get(c["x"], c["x"])} if c["x"] else {})}

    @staticmethod
    def _tokens(values: list[str], host: str) -> tuple[str, ...]:
        """Long random values, as they are and as a page may write them. (Not words: cutting those out could hide what a button says.)"""
        out = set()
        # (Also the random parts of one that has words in it, or other signs.)
        for v in [*values, *(x for v in values for x in [*re.findall(r"[A-Za-z0-9_-]{20,}", urllib.parse.unquote(v)), *re.findall(r"[A-Za-z0-9]{20,}", urllib.parse.unquote(v))])]:
            for x in {v, urllib.parse.unquote(v), urllib.parse.quote(v, safe=""), html.escape(v)}:
                if re.fullmatch(r"[A-Za-z0-9._~%/+=:&;-]{16,}", x) and re.search(r"\d", x) and re.search(r"[A-Za-z]", x) and host not in x \
                        and not re.search(r"(^|[-_/.+])[A-Z]?[a-z]{2,}[-_/.+][A-Z]?[a-z]{2,}($|[-_/.+])", x):
                    out.add(x)
        return tuple(sorted(out, key=len, reverse=True))

    def fill_session(self, req: dict) -> dict:
        host, path, given = req.get("host"), req.get("path"), req.get("cookies")
        engine = self._engine_for(req)
        if not isinstance(host, str) or not LOGIN_HOST.fullmatch(host) or not isinstance(path, str) or not COOKIE_PATH.fullmatch(path) \
                or ".." in path or "//" in path or not isinstance(given, list) or not 0 < len(given) <= 80:
            raise Refused("that session isn't as expected")
        cookies = [self._cookie(c, host) for c in given]
        cookies = [c for c in cookies if not CLEARANCE.fullmatch(c["name"])]
        if not cookies:
            raise Refused("there is nothing in that session that can be used")
        url = self.check_url(f"https://{host}{path}")
        s = self._new_session(engine)
        s.signed_in = s.filled = True  # from the first moment
        s.tokens = self._tokens([c["value"] for c in cookies if c["httpOnly"]], host)
        try:
            for c in cookies:  # one by one: an engine that refuses one may refuse them all together
                try:
                    s.cdp.call("Network.setCookie", c, s.cdp_session)
                except CdpProtocolError:
                    continue
            # (Or leave one out without a word.)
            there = s.cdp.call("Network.getCookies", {"urls": [f"https://{host}/", url]}, s.cdp_session).get("cookies") or []
            names = {(x.get("name"), str(x.get("domain") or "").lstrip(".")) for x in there if isinstance(x, dict)}
            kept = sum((c["name"], login_host_of(c, host)) in names for c in cookies)
            if not kept:
                raise Refused("the browser took nothing of that session")
            self._visit(s, url)
            s.cdp.call("Page.navigate", {"url": url}, s.cdp_session)
        except (Refused, CdpProtocolError) as e:
            self.drop()
            raise Refused(str(e) if isinstance(e, Refused) else "the browser couldn't take that session") from None
        try:
            self._settle(s, NAV_TIMEOUT)
        except CdpProtocolError:
            pass  # it is still loading: the next look tells
        return {"ok": True, "session": s.id, "kept": kept, "of": len(cookies)}

    def _moved(self, s: Session, top: dict) -> bool:
        now = self._frames(s)[0]
        return now["url"] != top["url"] or now["loader"] != top["loader"]

    def _set_all(self, s: Session, plan: list[tuple]) -> None:
        for frame, n, value in plan:
            r = self._run(s, frame, {"op": "set", "n": n, "value": value}) or {}
            if not r.get("ok"):
                raise Refused("a field disappeared while filling; check the page before trying again")

    def _pay_button(self, s: Session, button: Any, frames: list[dict]) -> tuple[dict, int]:
        changed = Refused("the page changed since the approval; nothing was filled")
        if not isinstance(button, dict) or not isinstance(button.get("binding"), dict):
            raise Refused("bad pay button")
        b = button["binding"]
        try:
            bframe, bd, bn = self._element(s, b.get("ref"), frames)
        except Refused:
            raise changed from None
        if (button.get("ref") != b.get("ref") or bframe["frame_id"] != b.get("frame_id")
                or bframe["origin"] != b.get("frame_origin") or bd.get("kind") != "button"
                or any(str(bd.get(k) or "")[:120] != str(b.get(k) or "") for k in self.BOUND)):
            raise changed
        return bframe, bn

    def pay(self, req: dict) -> dict:
        s = self._session(req)
        # 1. verify: nothing has been written; any refusal here is definite.
        self._phase = "verify"
        if s.engine != "obscura":
            raise Refused("card details don't go into this browser engine; nothing was filled or paid")
        frames, plan = self._verify(s, req, req.get("fields"), allow_card=True)
        bframe, bn = self._pay_button(s, req.get("button"), frames)
        snap = self.snapshot(s)
        if totals_key(snap["totals"]) != totals_key(req.get("expect_totals") or []):
            raise Refused("the totals on the page changed since the approval; nothing was filled or paid")
        if snap["risky_checked"]:
            raise Refused("a subscription / save-card / Link box is ticked; nothing was filled or paid")
        # 2. filled: from the first value on, this session only looks (no click or type can follow a half fill).
        self._acted = s.readonly = True
        self._phase = "filled"
        self._set_all(s, plan)
        self.sleep(PAY_FILL_SETTLE)  # let the provider's frame report the card as complete before pressing Pay
        cur = self._run(s, bframe, {"op": "inspect", "n": bn}) or {}
        if not cur.get("connected") or cur.get("disabled"):
            raise Refused("the card was filled but the pay button is gone or still disabled; Pay was NOT pressed. "
                          "Check the page")
        # 3. sent: Pay is pressed. Whatever happens after this, the outcome is unknown, never a definite refusal.
        self._phase = "sent"
        try:
            r = self._run(s, bframe, {"op": "click", "n": bn}) or {}
            if not r.get("ok"):
                return self._error("the pay button vanished while being pressed; the payment MAY have gone through")
            return self._after_pay(s, frames[0]["url"])
        except Refused:
            self.drop()
            return self._error("after pressing Pay the page went somewhere the checkout browser doesn't open (or "
                               "changed); the payment MAY have gone through")
        except Exception:
            self.retire = True
            self.drop()
            return self._error("the checkout browser failed after pressing Pay; the payment MAY have gone through")

    def _after_pay(self, s: Session, before: str) -> dict:
        start = self.clock()
        self.sleep(2)
        while self.clock() - start < PAY_SETTLE:
            main = self._frames(s)[0]
            st = self._run(s, main, {"op": "state"}, quiet=True) or {}
            if st.get("ready") == "complete" and main["url"] != before:
                break
            self.sleep(1)
        self._check_top(s)
        snap = self.snapshot(s)
        m = CONFIRMATION.search(snap["text"])
        snap["confirmation"] = snap["text"][max(0, m.start() - 150):m.start() + 250].strip()[:400] if m else ""
        return {"ok": True, "session": s.id, "snapshot": snap}


# --- the socket server -------------------------------------------------------------------------


def peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def read_line(conn: socket.socket) -> bytes:
    deadline = time.monotonic() + 10  # for all of it: this serves one caller at a time
    buf = b""
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise Refused("too slow")
        conn.settimeout(left)
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST:
            raise Refused("request too big")
    return buf.split(b"\n", 1)[0]


def serve_one(controller: Controller, conn: socket.socket, allowed_uids: set[int],
              uid_of: Callable[[socket.socket], int] = peer_uid, signin_uids: set[int] = frozenset()) -> None:
    try:
        try:
            line = read_line(conn)  # read first (bounded), so the peer always gets an answer, not a reset
        except (Refused, OSError):
            line = b""
        uid = uid_of(conn)
        if uid not in allowed_uids and uid not in signin_uids:
            resp = {"ok": False, "error": "not allowed"}
        else:
            try:
                req = json.loads(line or b"null")
            except ValueError:
                req = None
            resp = controller.handle(req, signin=uid in signin_uids)
        conn.settimeout(30)
        conn.sendall(json.dumps(resp, ensure_ascii=False).encode() + b"\n")
    except OSError:
        pass
    finally:
        conn.close()


def listening_socket() -> socket.socket:
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        return socket.socket(fileno=3)  # from ampi-checkout.socket: root owns the path, we can't re-bind it
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    s.listen(4)
    return s


def _uids(var: str) -> set[int]:
    return {int(x) for x in os.environ.get(var, "").split(",") if x.strip().isdigit()}


def main() -> int:
    if sys.argv[1:] != ["serve"]:
        print("usage: checkout.py serve", file=sys.stderr)
        return 2
    allowed = _uids("AMPI_CHECKOUT_CLIENT_UIDS")
    signin = _uids("AMPI_CHECKOUT_SIGNIN_UIDS") - allowed  # ampi-signin; nobody when it isn't installed
    engine = _uids("AMPI_CHECKOUT_ENGINE_UID")
    if not allowed or len(engine) != 1:
        print("AMPI_CHECKOUT_CLIENT_UIDS and AMPI_CHECKOUT_ENGINE_UID must be set", file=sys.stderr)
        return 2
    lp = _uids("AMPI_CHECKOUT_LP_UID")  # the second engine's user; none when it isn't installed
    controller = Controller(engine_uid=engine.pop(), signin=bool(signin), lp_uid=lp.pop() if len(lp) == 1 else None)
    srv = listening_socket()
    print("ampi-checkout controller ready", file=sys.stderr)
    while True:
        ready, _, _ = select.select([srv], [], [], 5)
        controller.expire()
        if ready and not should_exit(controller):
            conn, _ = srv.accept()
            serve_one(controller, conn, allowed, signin_uids=signin)
        if should_exit(controller):
            # A session paid or lost the engine: exit, so systemd stops the engine too (BindsTo=) and the next
            # request on the socket starts both fresh. The socket (Accept=no) keeps listening meanwhile.
            print("ampi-checkout controller: session over after a payment or engine failure; restarting fresh",
                  file=sys.stderr)
            return 0


def should_exit(controller: Controller) -> bool:
    return controller.retire and controller.session is None


if __name__ == "__main__":
    sys.exit(main())
