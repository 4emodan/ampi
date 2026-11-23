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
    /** A password here opens much more than one site; these also refuse or challenge an assistant. */
    private static final List<String> NOT = List.of("google.com", "gmail.com", "youtube.com", "apple.com", "icloud.com",
            "facebook.com", "microsoft.com", "microsoftonline.com", "live.com", "outlook.com", "office.com", "yahoo.com");
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
        for (String not : NOT) {
            if (s.equals(not) || s.endsWith("." + not)) {
                throw new IllegalArgumentException("Not for " + not + ": its password opens much more than one site");
            }
        }
        return s;
    }
}
