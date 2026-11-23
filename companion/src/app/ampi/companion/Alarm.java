package app.ampi.companion;

import android.content.Context;
import android.os.SystemClock;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.time.DayOfWeek;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.EnumSet;
import java.util.Locale;
import java.util.Set;

/**
 * One alarm or timer. A wall-clock alarm has {@code time} and a date or days; a timer, a snooze and a test only have the
 * moment {@code at} (and, within one boot, the same moment on the monotonic clock, which a corrected clock doesn't move).
 */
final class Alarm {
    static final String[] DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    String id = "", kind = "alarm", time = "", date = "", label = "", request = "", day = "";
    Set<DayOfWeek> days = EnumSet.noneOf(DayOfWeek.class);
    long at, created, elapsed;
    int boot = -1;
    boolean snoozed, ringing;

    boolean wallClock() {
        return !time.isEmpty();
    }

    boolean repeats() {
        return wallClock() && !days.isEmpty();
    }

    /** What makes two alarms the same one. */
    String identity() {
        return wallClock() ? time + "|" + date + "|" + days + "|" + label : "at|" + id;
    }

    /** Sets {@code at} to the next moment after now (-1: a date in the past). */
    void toNext(long now) {
        moment(AlarmTimes.next(LocalTime.parse(time), onlyDate(), days, ZoneId.systemDefault(), now));
    }

    /** The clock or zone changed, or the phone restarted: which moment is it now? */
    void recompute(Context c, long now) {
        if (wallClock()) {
            LocalDate of = !day.isEmpty() ? LocalDate.parse(day) : !date.isEmpty() ? LocalDate.parse(date)
                    : Instant.ofEpochMilli(at).atZone(ZoneId.systemDefault()).toLocalDate();
            moment(AlarmTimes.again(LocalTime.parse(time), onlyDate(), days, of, ZoneId.systemDefault(), now));
        } else if (boot == boots(c)) {
            at = now + (elapsed - SystemClock.elapsedRealtime());
        }
    }

    private void moment(long when) {
        at = when;
        if (when > 0) day = Instant.ofEpochMilli(when).atZone(ZoneId.systemDefault()).toLocalDate().toString();
    }

    /** A moment this far from now, whatever happens to the wall clock. */
    void in(Context c, long now, long ms) {
        at = now + ms;
        elapsed = SystemClock.elapsedRealtime() + ms;
        boot = boots(c);
    }

    private static int boots(Context c) {
        return Settings.Global.getInt(c.getContentResolver(), Settings.Global.BOOT_COUNT, -2);
    }

    private LocalDate onlyDate() {
        return date.isEmpty() ? null : LocalDate.parse(date);
    }

    JSONObject json() throws JSONException {
        JSONArray d = new JSONArray();
        for (DayOfWeek w : days) d.put(DAYS[w.getValue() - 1]);
        return new JSONObject().put("id", id).put("kind", kind).put("time", time).put("date", date).put("days", d)
                .put("label", label).put("at", at).put("created", created).put("snoozed", snoozed).put("ringing", ringing)
                .put("request", request).put("day", day).put("elapsed", elapsed).put("boot", boot);
    }

    /** What Ampi gets to see. */
    JSONObject shared() throws JSONException {
        JSONObject o = json();
        for (String k : new String[] {"request", "day", "elapsed", "boot"}) o.remove(k);
        return o;
    }

    static Alarm from(JSONObject o) throws JSONException {
        Alarm a = new Alarm();
        a.id = o.getString("id");
        a.kind = o.getString("kind");
        a.time = o.optString("time");
        a.date = o.optString("date");
        a.label = o.optString("label");
        a.request = o.optString("request");
        a.day = o.optString("day");
        a.at = o.getLong("at");
        a.created = o.optLong("created");
        a.elapsed = o.optLong("elapsed");
        a.boot = o.optInt("boot", -1);
        a.snoozed = o.optBoolean("snoozed");
        a.ringing = o.optBoolean("ringing");
        JSONArray d = o.optJSONArray("days");
        for (int i = 0; d != null && i < d.length(); i++) a.days.add(day(d.getString(i)));
        if (a.wallClock()) {
            LocalTime.parse(a.time);
            if (!a.date.isEmpty()) LocalDate.parse(a.date);
            if (!a.day.isEmpty()) LocalDate.parse(a.day);
        }
        return a;
    }

    static DayOfWeek day(String name) throws JSONException {
        for (int i = 0; i < DAYS.length; i++) if (DAYS[i].equals(name)) return DayOfWeek.of(i + 1);
        throw new JSONException("not a day: " + name);
    }

    /** "Tue 24 Nov 07:00" + days + label, for notices and lists. */
    String text() {
        String s = when(at);
        if (repeats()) {
            var names = new StringBuilder();
            for (DayOfWeek w : days) names.append(names.length() == 0 ? "" : ", ").append(DAYS[w.getValue() - 1]);
            s += days.size() == 7 ? ", every day" : ", every " + names;
        }
        if (snoozed) s += ", snoozed";
        return label.isEmpty() ? s : s + " “" + label + "”";
    }

    static String when(long at) {
        return DateTimeFormatter.ofPattern("EEE d MMM HH:mm", Locale.ENGLISH)
                .format(Instant.ofEpochMilli(at).atZone(ZoneId.systemDefault()));
    }
}
