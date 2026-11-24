package app.ampi.companion;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.time.DayOfWeek;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.util.EnumSet;
import java.util.Set;

/** Plain checks of the parts with no Android in them (run by tests/test_companion.py). */
final class LogicTest {
    static int failed;

    static void eq(String what, Object want, Object got) {
        if (!want.equals(got)) {
            failed++;
            System.out.println("FAIL " + what + ": wanted " + want + ", got " + got);
        }
    }

    static long t(String s) {
        return ZonedDateTime.parse(s).toInstant().toEpochMilli();
    }

    static String next(String time, String date, Set<DayOfWeek> days, String zone, String now) {
        long n = AlarmTimes.next(LocalTime.parse(time), date == null ? null : LocalDate.parse(date), days,
                ZoneId.of(zone), t(now));
        return n < 0 ? "none" : Instant.ofEpochMilli(n).atZone(ZoneId.of(zone)).toOffsetDateTime().toString();
    }

    static void alarmTimes() {
        String z = "Europe/Helsinki";
        eq("later today", "2026-11-24T07:00+02:00", next("07:00", null, null, z, "2026-11-24T06:59:59+02:00"));
        eq("now is past", "2026-11-25T07:00+02:00", next("07:00", null, null, z, "2026-11-24T07:00:00+02:00"));
        eq("date", "2026-12-01T18:30+02:00", next("18:30", "2026-12-01", null, z, "2026-11-24T07:00:00+02:00"));
        eq("date past", "none", next("18:30", "2026-11-01", null, z, "2026-11-24T07:00:00+02:00"));
        Set<DayOfWeek> thu = EnumSet.of(DayOfWeek.THURSDAY), tue = EnumSet.of(DayOfWeek.TUESDAY);
        eq("days", "2026-11-26T07:00+02:00", next("07:00", null, thu, z, "2026-11-24T08:00:00+02:00"));
        eq("same weekday, past", "2026-12-01T07:00+02:00", next("07:00", null, tue, z, "2026-11-24T08:00:00+02:00"));
        eq("spring gap", "2027-03-28T04:30+03:00", next("03:30", "2027-03-28", null, z, "2027-03-27T12:00:00+02:00"));
        eq("autumn overlap", "2026-10-25T03:30+03:00", next("03:30", "2026-10-25", null, z, "2026-10-24T12:00:00+03:00"));
        eq("overlap once", "2026-10-26T03:30+02:00", next("03:30", null, null, z, "2026-10-25T03:30:00+03:00"));
        eq("other zone", "2026-11-25T07:00+09:00", next("07:00", null, null, "Asia/Tokyo", "2026-11-24T23:30:00+02:00"));
    }

    static String again(String time, String date, Set<DayOfWeek> days, String day, String zone, String now) {
        long n = AlarmTimes.again(LocalTime.parse(time), date == null ? null : LocalDate.parse(date), days, LocalDate.parse(day),
                ZoneId.of(zone), t(now));
        return n < 0 ? "none" : Instant.ofEpochMilli(n).atZone(ZoneId.of(zone)).toOffsetDateTime().toString();
    }

    static void clockChanges() {
        Set<DayOfWeek> all = EnumSet.allOf(DayOfWeek.class);
        String z = "Europe/Helsinki";
        eq("nothing changed", "2026-11-25T07:00+02:00", again("07:00", null, all, "2026-11-25", z, "2026-11-24T12:00:00+02:00"));
        eq("jumped over: that one, in the past", "2026-11-24T07:00+09:00", again("07:00", null, all, "2026-11-24", "Asia/Tokyo", "2026-11-24T06:00:00+02:00"));
        eq("one-off jumped over", "2026-11-24T07:00+09:00", again("07:00", "2026-11-24", Set.of(), "2026-11-24", "Asia/Tokyo", "2026-11-24T06:00:00+02:00"));
        eq("clock went back two days", "2026-11-23T07:00+02:00", again("07:00", null, all, "2026-11-25", z, "2026-11-22T12:00:00+02:00"));
        eq("zone moved west", "2026-11-25T07:00Z", again("07:00", null, all, "2026-11-25", "UTC", "2026-11-24T22:30:00+02:00"));
    }

    static String frame(int... b) {
        byte[] bytes = new byte[b.length];
        for (int i = 0; i < b.length; i++) bytes[i] = (byte) b[i];
        try {
            return String.valueOf(Frames.read(new ByteArrayInputStream(bytes)));
        } catch (IOException e) {
            return "error: " + e.getMessage();
        }
    }

    static void frames() throws IOException {
        var out = new ByteArrayOutputStream();
        Frames.write(out, "{\"a\":\"é\"}");
        Frames.write(out, "x");
        var in = new ByteArrayInputStream(out.toByteArray());
        eq("frame 1", "{\"a\":\"é\"}", Frames.read(in));
        eq("frame 2", "x", Frames.read(in));
        eq("end", "null", String.valueOf(Frames.read(in)));
        eq("zero", "error: bad frame length 0", frame(0, 0, 0, 0));
        eq("negative", "error: bad frame length -1", frame(255, 255, 255, 255));
        eq("too big", "error: bad frame length 65537", frame(0, 1, 0, 1));
        eq("cut body", "error: cut frame", frame(0, 0, 0, 2, 'x'));
        eq("cut head", "error: cut frame", frame(0, 0));
        eq("bad utf-8", "error: not UTF-8", frame(0, 0, 0, 1, 0xff));
        try {
            Frames.write(out, "x".repeat(Frames.MAX + 1));
            eq("write too big", "error", "written");
        } catch (IOException expected) {
            // fine
        }
    }

    static void texts() {
        eq("line", "a b c", Texts.line(" a\nb\tc ", 20));
        eq("bidi and zero width", "abc", Texts.line("a\u202eb\u200bc\u2066", 20));
        eq("lines", "a\nb", Texts.lines("a\r\nb\n", 20));
        eq("cut", "ab", Texts.line("abc", 2));
        eq("emoji is one character", "\ud83d\ude00\ud83d\ude00", Texts.line("\ud83d\ude00\ud83d\ude00\ud83d\ude00", 2));
        eq("lone surrogate", "ab", Texts.line("a\ud83db", 20));
        eq("null", "", Texts.line(null, 2));
    }

    static String hosts(String typed) {
        try {
            return String.join(" ", Hosts.parse(typed));
        } catch (IllegalArgumentException e) {
            return "no";
        }
    }

    static void sites() {
        eq("plain", "example.com", hosts(" Example.com "));
        eq("link", "shop.example.com", hosts("https://www.shop.example.com:443/login?x=1#y"));
        eq("several", "example.com login.example.com", hosts("example.com,\nlogin.example.com example.com"));
        for (String bad : new String[] {"", " ", "example", "http://example.com", "ftp://example.com", "example.com:8443", "a@example.com",
                "https://a:b@example.com", "1.2.3.4", "https://[::1]/", "example.com.", "ex\\ample.com", "-a.com", "a-.com", "a..com",
                "ex ample", "xn--e1afmkfd", "пример.рф", "faß.de", "example.com\u0000", "accounts.google.com", "google.com",
                "https://www.facebook.com/", "a.com b.com c.com d.com e.com f.com", "a".repeat(64) + ".com", "a.".repeat(130) + "com"}) {
            eq("site " + bad, "no", hosts(bad));
        }
        eq("not google", "notgoogle.com", hosts("notgoogle.com"));
        eq("punycode", "xn--e1afmkfd.xn--p1ai", hosts("xn--e1afmkfd.xn--p1ai"));
        eq("newline at the end", false, Hosts.valid("example.com\n"));
    }

    static String code(String key, long t) throws Exception {
        try {
            Totp x = Totp.parse(key);
            Totp again = Totp.parse(x.save());  // what is kept in a login
            eq("kept key", x.code(x.window(t)), again.code(again.window(t)));
            return x.code(x.window(t));
        } catch (IllegalArgumentException e) {
            return "no";
        }
    }

    static void codes() throws Exception {
        // RFC 6238, appendix B
        String k20 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", k32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA",
                k64 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNA";
        for (long[] x : new long[][] {{59, 94287082}, {1111111109, 7081804}, {1234567890, 89005924}, {20000000000L, 65353130}}) {
            eq("SHA1 at " + x[0], String.format("%08d", x[1]), code("otpauth://totp/a?secret=" + k20 + "&digits=8", x[0]));
        }
        eq("SHA256", "46119246", code("otpauth://totp/a%20b:c?digits=8&secret=" + k32 + "&algorithm=sha256&issuer=x", 59));
        eq("SHA512", "90693936", code("otpauth://totp/a?digits=8&algorithm=SHA512&secret=" + k64, 59));
        eq("only letters", "287082", code("gezd gnbv gy3t qojq-gezd gnbv gy3t qojq", 59));
        eq("longer period", "287082", code("otpauth://totp/a?period=60&secret=" + k20, 100));
        for (String bad : new String[] {"otpauth://hotp/a?secret=" + k20 + "&counter=1", "otpauth://totp/a", "GEZDGNBV", "not a key!", "otpauth://totp/a?secret=" + k20 + "&digits=5",
                "otpauth://totp/a?algorithm=MD5&secret=" + k20, "otpauth://totp/a?period=0&secret=" + k20, "steam://x"}) eq("key " + bad, "no", code(bad, 59));
        eq("none", true, Totp.parse("  ") == null);
        for (String[] x : new String[][] {{" 123 456 ", "123456"}, {"G-123456", "G-123456"}, {"abcd\u2013efgh", "abcd-efgh"}, {"123", null}, {"12 34!", null},
                {"-1234", null}, {"", null}, {"1234567890abcdef", "1234567890abcdef"}, {"1234567890abcdefg", null}}) {
            eq("typed " + x[0], String.valueOf(x[1]), String.valueOf(Totp.typed(x[0])));
        }
    }

    public static void main(String[] a) throws Exception {
        if (a.length > 0) {
            System.out.println(Texts.pairCode("server".getBytes(), "phone".getBytes(), null));
            System.out.println(Texts.pairCode("server".getBytes(), "phone".getBytes(), "key".getBytes()));
            System.out.println(String.join(" ", Hosts.NOT));
            for (int i = 2; i < a.length; i++) System.out.println(Hosts.refused(a[i]));
            return;
        }
        alarmTimes();
        clockChanges();
        frames();
        texts();
        sites();
        codes();
        System.out.println(failed == 0 ? "ok" : failed + " failed");
        System.exit(failed == 0 ? 0 : 1);
    }
}
