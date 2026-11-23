package app.ampi.companion;

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
 * One alarm or timer. A wall-clock alarm has {@code time} (and a date, or days, or neither); a timer, a snooze and a test
 * only have the moment {@code at}.
 */
final class Alarm {
    static final String[] DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    String id = "", kind = "alarm", time = "", date = "", label = "", request = "";
    Set<DayOfWeek> days = EnumSet.noneOf(DayOfWeek.class);
    long at, created;
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

    /** The next moment after now, or -1 (a date in the past). */
    long next(long now, ZoneId zone) {
        return AlarmTimes.next(LocalTime.parse(time), date.isEmpty() ? null : LocalDate.parse(date), days, zone, now);
    }

    JSONObject json() throws JSONException {
        JSONArray d = new JSONArray();
        for (DayOfWeek w : days) d.put(DAYS[w.getValue() - 1]);
        return new JSONObject().put("id", id).put("kind", kind).put("time", time).put("date", date).put("days", d)
                .put("label", label).put("at", at).put("created", created).put("snoozed", snoozed).put("ringing", ringing)
                .put("request", request);
    }

    static Alarm from(JSONObject o) throws JSONException {
        Alarm a = new Alarm();
        a.id = o.getString("id");
        a.kind = o.getString("kind");
        a.time = o.optString("time");
        a.date = o.optString("date");
        a.label = o.optString("label");
        a.request = o.optString("request");
        a.at = o.getLong("at");
        a.created = o.optLong("created");
        a.snoozed = o.optBoolean("snoozed");
        a.ringing = o.optBoolean("ringing");
        JSONArray d = o.optJSONArray("days");
        for (int i = 0; d != null && i < d.length(); i++) a.days.add(day(d.getString(i)));
        if (a.wallClock()) {
            LocalTime.parse(a.time);
            if (!a.date.isEmpty()) LocalDate.parse(a.date);
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
