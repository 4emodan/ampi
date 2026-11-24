package app.ampi.companion;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/** Text cleaning and the pairing code (no Android in here: tested on the desktop). */
final class Texts {
    private Texts() {}

    /** One line: no control, bidi or zero-width characters, cut to max characters. */
    static String line(String s, int max) {
        return clean(s, false, max);
    }

    static String lines(String s, int max) {
        return clean(s, true, max);
    }

    private static String clean(String s, boolean newlines, int max) {
        if (s == null) return "";
        var out = new StringBuilder();
        s.replace("\r\n", "\n").codePoints().forEach(cp -> {
            if (cp == '\n' && newlines) {
                out.append('\n');
                return;
            }
            switch (Character.getType(cp)) {
                case Character.CONTROL, Character.LINE_SEPARATOR, Character.PARAGRAPH_SEPARATOR -> out.append(' ');
                case Character.FORMAT, Character.SURROGATE, Character.PRIVATE_USE, Character.UNASSIGNED -> { }
                default -> out.appendCodePoint(cp);
            }
        });
        String t = out.toString().strip();
        return t.codePointCount(0, t.length()) <= max ? t : t.substring(0, t.offsetByCodePoints(0, max));
    }

    /** What looks wrong about a login before it is saved ("" if nothing): a password manager may have filled in the wrong fields. */
    static String loginProblem(String label, String user, String secret) {
        if (user.isEmpty()) return "Enter the user name or email";
        if (secret.isEmpty()) return "Enter the password";
        if (user.length() > 200 || secret.length() > 200) return "At most 200 characters each";
        if (user.equals(secret)) return "User name and password are the same. Were both filled in from the wrong place?";
        if (!email(user) && email(secret)) return "There is an email in the password field. Are the two swapped?";
        if (!email(user) && email(label)) return "There is an email in the label field. Was it meant for \"User name or email\"?";
        if (user.indexOf('@') > 0 && !email(user)) return "That email isn't complete, or a letter in it is from another alphabet";
        return "";
    }

    private static boolean email(String s) {
        return s.matches("[!-?A-~]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+");  // ASCII without '@'
    }

    /** The same code as `ampi-phone code` on the server: 130 bits over the three certificates. */
    static String pairCode(byte[] server, byte[] phone, byte[] signin) {
        String text = "ampi-pair-v2\n" + part(server) + "\n" + part(phone) + "\n" + part(signin) + "\n";
        String code = base32(sha256(text.getBytes(StandardCharsets.UTF_8))).substring(0, 26);
        return code.substring(0, 5) + " " + code.substring(5, 10) + " " + code.substring(10, 15) + " "
                + code.substring(15, 20) + " " + code.substring(20);
    }

    private static String part(byte[] b) {
        if (b == null || b.length == 0) return "-";
        var hex = new StringBuilder();
        for (byte x : sha256(b)) hex.append(String.format("%02x", x));
        return hex.toString();
    }

    private static byte[] sha256(byte[] b) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(b);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static String base32(byte[] b) {
        var out = new StringBuilder();
        int buffer = 0, bits = 0;
        for (byte x : b) {
            buffer = (buffer << 8) | (x & 0xff);
            bits += 8;
            while (bits >= 5) {
                out.append("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567".charAt((buffer >> (bits - 5)) & 31));
                bits -= 5;
            }
            buffer &= (1 << bits) - 1;
        }
        return out.toString();
    }
}
