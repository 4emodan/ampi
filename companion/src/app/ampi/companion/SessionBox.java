package app.ampi.companion;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * What the owner's Firefox extension hands over: the cookies of one site, encrypted (companion/firefox/logic.js). What it is about
 * is readable and authenticated along with them; nothing in it counts before that (no Android in here: tested on the desktop).
 */
record SessionBox(String host, String path, long at, String id, int count, String scope, byte[] iv, byte[] data, byte[] bound) {
    static final long GOOD_FOR = 10 * 60_000, AHEAD = 60_000;
    static final int MAX = 48_000;
    private static final String TAG = "ampi-session1";
    private static final Pattern TEXT = Pattern.compile("ampi-session1\\.([A-Za-z0-9_-]{20,1200})\\.([A-Za-z0-9_-]{60,64000})(?![A-Za-z0-9_-])");
    private static final Pattern PATH = Pattern.compile("/[A-Za-z0-9._~%/+-]{0,200}"), ID = Pattern.compile("[0-9a-f]{32}");

    /** @throws IllegalArgumentException with the reason, for the owner */
    static SessionBox parse(String text, long now) {
        Matcher m = TEXT.matcher(text == null ? "" : text);
        if (!m.find()) throw new IllegalArgumentException("That isn't a session from Firefox.");
        byte[] sealed;
        String head;
        try {
            head = new String(Base64.getUrlDecoder().decode(m.group(1)), StandardCharsets.UTF_8);
            sealed = Base64.getUrlDecoder().decode(m.group(2));
        } catch (IllegalArgumentException e) {
            throw new IllegalArgumentException("That session can't be read.");
        }
        String[] h = head.split("\n", -1);
        if (h.length != 6 || sealed.length < 29 || sealed.length > MAX) throw new IllegalArgumentException("That session can't be read.");
        String host = h[0], path = h[1], id = h[3], scope = h[5];
        long at;
        int count;
        try {
            at = Long.parseLong(h[2]);
            count = Integer.parseInt(h[4]);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("That session can't be read.");
        }
        if (!Hosts.valid(host) || !PATH.matcher(path).matches() || path.contains("..") || path.contains("//") || !ID.matcher(id).matches()
                || count < 1 || count > 80 || !(scope.isEmpty() || Hosts.valid(scope) && (host.equals(scope) || host.endsWith("." + scope)))) {
            throw new IllegalArgumentException("That session can't be read.");
        }
        if (Hosts.refused(site(host))) throw new IllegalArgumentException("Not for " + host + ": mail, sign-in providers, banks and the like open too much.");
        if (at < now - GOOD_FOR || at > now + AHEAD) throw new IllegalArgumentException("That session is too old: give it in Firefox again.");
        byte[] iv = java.util.Arrays.copyOf(sealed, 12), data = java.util.Arrays.copyOfRange(sealed, 12, sealed.length);
        return new SessionBox(host, path, at, id, count, scope, iv, data, (TAG + "\n" + head).getBytes(StandardCharsets.UTF_8));
    }

    static String site(String host) {
        return host.startsWith("www.") ? host.substring(4) : host;
    }

    String site() {
        return site(host);
    }

    boolean fresh(long now) {
        return at >= now - GOOD_FOR && at <= now + AHEAD;
    }
}
