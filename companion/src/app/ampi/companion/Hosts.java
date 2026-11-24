package app.ampi.companion;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/** Which sites a saved login is for: exact host names (no Android in here: tested on the desktop). */
final class Hosts {
    /** The same as HOST_RE in ampi_phone/signin.py. */
    private static final Pattern HOST = Pattern.compile(
            "(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])");
    /** The same as NOT and NOT_WORDS in ampi_phone/signin.py: one of these opens much more than one site, is how Ampi itself is
     *  controlled, or is about money. */
    static final List<String> NOT = List.of((
            "google.com gmail.com googlemail.com youtube.com apple.com icloud.com facebook.com instagram.com "
            + "threads.net threads.com messenger.com whatsapp.com meta.com microsoft.com microsoftonline.com live.com "
            + "outlook.com hotmail.com office.com office365.com azure.com yahoo.com linkedin.com x.com "
            + "twitter.com telegram.org t.me telegram.me web.telegram.org fragment.com proton.me protonmail.com "
            + "tuta.com fastmail.com zoho.com gmx.com gmx.net gmx.de web.de mail.com "
            + "mail.ru vk.com vk.ru yandex.ru yandex.com ya.ru github.com gitlab.com "
            + "bitbucket.org npmjs.com pypi.org docker.com hetzner.com hetzner.cloud hetzner.de digitalocean.com "
            + "aws.amazon.com amazonaws.com awsapps.com cloudflare.com ovh.com ovhcloud.com linode.com akamai.com "
            + "namecheap.com godaddy.com gandi.net porkbun.com netlify.com vercel.com heroku.com anthropic.com "
            + "claude.ai claude.com openai.com chatgpt.com mozilla.org firefox.com 1password.com 1password.eu "
            + "1password.ca bitwarden.com bitwarden.eu lastpass.com dashlane.com nordpass.com keepersecurity.com okta.com "
            + "auth0.com onelogin.com duosecurity.com paypal.com stripe.com klarna.com klarna.se revolut.com "
            + "wise.com n26.com monzo.com skrill.com neteller.com payoneer.com venmo.com cash.app "
            + "curve.com viva.com binance.com coinbase.com kraken.com bybit.com okx.com bitstamp.net "
            + "crypto.com metamask.io interactivebrokers.com interactivebrokers.ie degiro.eu degiro.nl nordnet.fi nordnet.se "
            + "etoro.com trading212.com nordea.fi nordea.com op.fi aktia.fi handelsbanken.se handelsbanken.fi "
            + "swedbank.se seb.se zoho.eu tutanota.com hey.com mailbox.org posteo.de aol.com "
            + "bunq.com suomi.fi").split(" "));
    private static final List<String> NOT_LABELS = List.of("google", "yahoo", "yandex", "hotmail", "gov", "govt", "gob", "gouv");
    private static final Pattern NOT_WORDS = Pattern.compile("bank|banque|banco|pankki|sparkasse");
    static final int MAX = 5;

    private Hosts() {}

    static boolean valid(String host) {
        return host != null && HOST.matcher(host).matches();
    }

    /** What the owner typed or pasted (hosts or https links, separated by spaces, commas or lines) as host names.
     *  @throws IllegalArgumentException with the reason, for the owner */
    static List<String> parse(String typed) {
        List<String> out = new ArrayList<>();
        for (String part : typed.strip().split("[\\s,]+")) {
            if (part.isEmpty()) continue;
            String h = one(part);
            if (!out.contains(h)) out.add(h);
        }
        if (out.isEmpty()) throw new IllegalArgumentException("Enter the site, e.g. example.com");
        if (out.size() > MAX) throw new IllegalArgumentException("At most " + MAX + " addresses");
        return out;
    }

    private static String one(String part) {
        String s = part.toLowerCase(Locale.ROOT);
        int scheme = s.indexOf("://");
        if (scheme >= 0) {
            if (!s.startsWith("https://")) throw new IllegalArgumentException("Only https:// sites");
            s = s.substring(8);
        }
        for (char end : new char[] {'/', '?', '#'}) {
            int i = s.indexOf(end);
            if (i >= 0) s = s.substring(0, i);
        }
        if (s.endsWith(":443")) s = s.substring(0, s.length() - 4);
        if (s.startsWith("www.")) s = s.substring(4);
        if (!valid(s)) throw new IllegalArgumentException("Not a site address: " + Texts.line(part, 60));
        if (refused(s)) throw new IllegalArgumentException("Not for " + s + ": mail, sign-in providers, banks and the like open too much");
        return s;
    }

    static boolean refused(String host) {
        for (String not : NOT) if (host.equals(not) || host.endsWith("." + not)) return true;
        for (String label : host.split("\\.")) if (NOT_LABELS.contains(label)) return true;
        int last = host.lastIndexOf('.');
        return last > 0 && NOT_WORDS.matcher(host.substring(0, last)).find();
    }
}
