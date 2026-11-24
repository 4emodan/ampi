package app.ampi.companion;

import java.io.ByteArrayOutputStream;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.util.Arrays;
import java.util.Locale;
import java.util.regex.Pattern;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** One-time codes: what an authenticator app does (RFC 6238), and codes the owner types (no Android in here: tested on the
 *  desktop). */
final class Totp {
    private static final Pattern TYPED = Pattern.compile("[A-Za-z0-9][A-Za-z0-9-]{2,14}[A-Za-z0-9]");
    private static final String BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

    private final byte[] key;
    private final String hash;  // SHA1, SHA256, SHA512
    private final int digits;
    final int period;

    private Totp(byte[] key, String hash, int digits, int period) {
        this.key = key;
        this.hash = hash;
        this.digits = digits;
        this.period = period;
    }

    /** A code as the owner typed it, or null when it can't be one. The same as CODE_RE in ampi_phone/signin.py. */
    static String typed(String s) {
        String code = s.replaceAll("[\\s -‏]", "").replaceAll("[‐-―]", "-");
        return TYPED.matcher(code).matches() ? code : null;
    }

    /** The key as a site shows it (letters, or an otpauth:// link); null for none.
     *  @throws IllegalArgumentException with the reason, for the owner */
    static Totp parse(String typed) {
        String s = typed.strip();
        if (s.isEmpty()) return null;
        String secret = s, hash = "SHA1";
        int digits = 6, period = 30;
        if (s.toLowerCase(Locale.ROOT).startsWith("otpauth://")) {
            if (!s.toLowerCase(Locale.ROOT).startsWith("otpauth://totp/")) throw new IllegalArgumentException("Only time-based codes (totp)");
            secret = "";
            int q = s.indexOf('?');
            for (String p : (q < 0 ? "" : s.substring(q + 1)).split("&")) {
                int eq = p.indexOf('=');
                if (eq < 0) continue;
                String v;
                try {
                    v = URLDecoder.decode(p.substring(eq + 1), StandardCharsets.UTF_8);
                } catch (IllegalArgumentException e) {
                    v = "";  // a bare % (in a name, most likely)
                }
                switch (p.substring(0, eq).toLowerCase(Locale.ROOT)) {
                    case "secret" -> secret = v;
                    case "algorithm" -> hash = v.toUpperCase(Locale.ROOT).replace("-", "");
                    case "digits" -> digits = number(v);
                    case "period" -> period = number(v);
                    default -> { }
                }
            }
        }
        if (!hash.equals("SHA1") && !hash.equals("SHA256") && !hash.equals("SHA512")) throw new IllegalArgumentException("That kind of authenticator key isn't supported");
        if (digits < 6 || digits > 8 || period < 15 || period > 120) throw new IllegalArgumentException("That kind of authenticator key isn't supported");
        byte[] key = base32(secret);
        if (key.length < 10 || key.length > 128) throw new IllegalArgumentException("That isn't an authenticator key: letters A-Z and digits 2-7, as the site shows it");
        return new Totp(key, hash, digits, period);
    }

    private static int number(String v) {
        try {
            return Integer.parseInt(v);
        } catch (NumberFormatException e) {
            return -1;
        }
    }

    private static byte[] base32(String s) {
        var out = new ByteArrayOutputStream();
        int bits = 0, value = 0;
        for (char ch : s.toUpperCase(Locale.ROOT).replaceAll("[\\s-]", "").replaceAll("=+$", "").toCharArray()) {
            int v = BASE32.indexOf(ch);
            if (v < 0) return new byte[0];
            value = (value << 5 | v) & 0xfff;
            bits += 5;
            if (bits >= 8) {
                bits -= 8;
                out.write(value >> bits & 0xff);
            }
        }
        return out.toByteArray();
    }

    /** For the encrypted record; {@link #parse} reads it again. */
    String save() {
        var b = new StringBuilder();
        int bits = 0, value = 0;
        for (byte x : key) {
            value = (value << 8 | x & 0xff) & 0xffff;
            bits += 8;
            while (bits >= 5) {
                bits -= 5;
                b.append(BASE32.charAt(value >> bits & 31));
            }
        }
        if (bits > 0) b.append(BASE32.charAt(value << 5 - bits & 31));
        return "otpauth://totp/x?secret=" + b + "&algorithm=" + hash + "&digits=" + digits + "&period=" + period;
    }

    long window(long epochSeconds) {
        return Math.floorDiv(epochSeconds, (long) period);
    }

    String code(long window) throws GeneralSecurityException {
        byte[] counter = new byte[8];
        for (int i = 0; i < 8; i++) counter[7 - i] = (byte) (window >>> 8 * i);
        Mac mac = Mac.getInstance("Hmac" + hash);
        mac.init(new SecretKeySpec(key, "RAW"));
        byte[] h = mac.doFinal(counter);
        int at = h[h.length - 1] & 15;
        int n = (h[at] & 127) << 24 | (h[at + 1] & 255) << 16 | (h[at + 2] & 255) << 8 | h[at + 3] & 255;
        int mod = digits == 6 ? 1_000_000 : digits == 7 ? 10_000_000 : 100_000_000;
        return String.format(Locale.ROOT, "%0" + digits + "d", n % mod);
    }

    void wipe() {
        Arrays.fill(key, (byte) 0);
    }
}
