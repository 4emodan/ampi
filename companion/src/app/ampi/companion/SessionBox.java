package app.ampi.companion;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.util.Arrays;
import java.util.Base64;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * What the owner's Firefox extension hands over: the cookies of one site, sealed (companion/firefox/logic.js). A random value is
 * wrapped for the app's private key; it and the secret only the extension has make the key of the rest. What it is about is
 * readable and authenticated along with it; nothing in it counts before that (no Android in here: tested on the desktop).
 */
record SessionBox(String host, long at, String id, int count, String scope, byte[] wrapped, byte[] iv, byte[] data, byte[] bound) {
    static final long GOOD_FOR = 10 * 60_000, AHEAD = 60_000;
    static final int MAX = 48_000, MAX_TEXT = 70_000;
    private static final int RSA = 256;
    private static final String TAG = "ampi-session2";
    private static final Pattern TEXT = Pattern.compile("ampi-session2\\.([A-Za-z0-9_-]{20,1200})\\.([A-Za-z0-9_-]{60,66000})(?![A-Za-z0-9_-])");
    private static final Pattern ID = Pattern.compile("[0-9a-f]{32}");

    /** @throws IllegalArgumentException with the reason, for the owner */
    static SessionBox parse(String text, long now) {
        Matcher m = TEXT.matcher(text == null || text.length() > MAX_TEXT ? "" : text);
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
        if (h.length != 5 || sealed.length < RSA + 29 || sealed.length > MAX) throw new IllegalArgumentException("That session can't be read.");
        String host = h[0], id = h[2], scope = h[4];
        long at;
        int count;
        try {
            at = Long.parseLong(h[1]);
            count = Integer.parseInt(h[3]);
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("That session can't be read.");
        }
        if (!Hosts.valid(host) || !ID.matcher(id).matches() || count < 1 || count > 80
                || !(scope.isEmpty() || Hosts.valid(scope) && (host.equals(scope) || host.endsWith("." + scope)))) {
            throw new IllegalArgumentException("That session can't be read.");
        }
        if (Hosts.refused(site(host))) throw new IllegalArgumentException("Not for " + host + ": mail, sign-in providers, banks and the like open too much.");
        if (at < now - GOOD_FOR || at > now + AHEAD) throw new IllegalArgumentException("That session is too old: give it in Firefox again.");
        return new SessionBox(host, at, id, count, scope, Arrays.copyOf(sealed, RSA), Arrays.copyOfRange(sealed, RSA, RSA + 12),
                Arrays.copyOfRange(sealed, RSA + 12, sealed.length), (TAG + "\n" + head).getBytes(StandardCharsets.UTF_8));
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

    /** What the extension's secret is used on, to make the key: what it is about, and what was wrapped. */
    byte[] forKey(byte[] random) {
        return ByteBuffer.allocate(bound.length + 1 + random.length).put(bound).put((byte) '\n').put(random).array();
    }

    byte[] open(byte[] key) throws GeneralSecurityException {
        Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
        c.init(Cipher.DECRYPT_MODE, new SecretKeySpec(key, "AES"), new GCMParameterSpec(128, iv));
        c.updateAAD(bound);
        return c.doFinal(data);
    }
}
