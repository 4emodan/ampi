package app.ampi.companion;

import android.app.AlarmManager;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.media.AudioManager;
import android.net.Uri;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.security.SecureRandom;
import java.time.LocalDate;
import java.time.LocalTime;
import java.time.ZoneId;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;

/** Setting, cancelling, ringing. The stored file is the truth; Android's alarm clock is set from it. */
final class Alarms {
    static final int MAX = 50, MAX_PER_DAY = 20;
    static final long LATE = 10 * 60_000L, SNOOZE = 10 * 60_000L, DAY = 24 * 3600_000L, EARLY = 1500;
    static final String FIRE = "app.ampi.companion.FIRE", UNDO_SET = "app.ampi.companion.UNDO_SET",
            UNDO_CANCEL = "app.ampi.companion.UNDO_CANCEL", AT = "at", TEXT = "text";
    private static final SecureRandom RANDOM = new SecureRandom();

    private Alarms() {}

    // --- asked for by Ampi -----------------------------------------------------------------------------

    /** A wall-clock alarm, or a timer with its moment already set. */
    static JSONObject set(Context c, String request, Alarm wanted) throws IOException, AlarmStore.Refused, JSONException {
        long now = System.currentTimeMillis();
        boolean[] existed = {false};
        Alarm done = AlarmStore.change(c, s -> {
            if (s.requests.contains(request)) {  // the same request again: the answer comes from what is stored
                for (Alarm a : s.alarms) if (a.request.equals(request)) return a;
                throw new AlarmStore.Refused("gone_again");
            }
            for (Alarm a : s.alarms) {
                if (wanted.wallClock() && !a.snoozed && a.identity().equals(wanted.identity())) {
                    if (a.ringing && !a.repeats()) throw new AlarmStore.Refused("ringing");
                    if (a.at <= now && !a.repeats()) throw new AlarmStore.Refused("past");
                    existed[0] = true;
                    if (!a.ringing && a.at > now) arm(c, a);  // set again, in case Android lost it (a due one is left alone)
                    return a;
                }
            }
            s.sets.removeIf(t -> t <= now - DAY || t > now + DAY);
            if (s.sets.size() >= MAX_PER_DAY) throw new AlarmStore.Refused("limit_day");
            if (s.alarms.size() >= MAX) throw new AlarmStore.Refused("limit_alarms");
            wanted.id = newId(s);
            wanted.created = now;
            wanted.request = request;
            if (wanted.wallClock()) wanted.toNext(now);
            if (wanted.at <= now) throw new AlarmStore.Refused("past");
            arm(c, wanted);  // first: if Android refuses, nothing is stored as set
            s.alarms.add(wanted);
            s.sets.add(now);
            s.request(request);
            s.event("set", wanted);
            return wanted;
        });
        if (done == wanted) {
            Notes.change(c, done.id, "Ampi set " + (done.kind.equals("timer") ? "a timer" : "an alarm"), done.text(),
                    "Undo", action(c, UNDO_SET, done));
        }
        long at = done.at;
        if (done.repeats() && at <= now) at = AlarmTimes.next(LocalTime.parse(done.time), null, done.days, ZoneId.systemDefault(), now);
        return new JSONObject().put("id", done.id).put("at", at).put("zone", ZoneId.systemDefault().getId())
                .put("existed", existed[0]).put("warn", warnings(c));
    }

    static void cancel(Context c, String request, String id) throws IOException, AlarmStore.Refused {
        Alarm gone = AlarmStore.change(c, s -> {
            if (s.requests.contains(request)) return null;  // the same request again: done before (perhaps undone since)
            Alarm a = s.find(id);
            if (a == null) throw new AlarmStore.Refused("no_such_alarm");
            if (a.ringing) throw new AlarmStore.Refused("ringing");
            s.alarms.remove(a);
            s.trash(a);
            s.request(request);
            s.event("cancelled", a);
            return a;
        });
        if (gone == null) return;
        disarm(c, gone);
        Notes.change(c, id, "Ampi cancelled " + (gone.kind.equals("timer") ? "a timer" : "an alarm"), gone.text(),
                "Undo", action(c, UNDO_CANCEL, gone));
    }

    static JSONObject list(Context c) throws JSONException, IOException {
        AlarmStore s = AlarmStore.load(c);  // unreadable must not look like "no alarms"
        JSONArray rows = new JSONArray();
        for (Alarm a : sorted(s.alarms)) rows.put(a.shared());
        return new JSONObject().put("alarms", rows).put("events", new JSONArray(s.events))
                .put("zone", ZoneId.systemDefault().getId()).put("warn", warnings(c));
    }

    static List<Alarm> sorted(List<Alarm> in) {
        List<Alarm> out = new ArrayList<>(in);
        out.sort((x, y) -> Long.compare(x.at, y.at));
        return out;
    }

    /** What would keep an alarm from being noticed right now, as codes (the server has the words). */
    static JSONArray warnings(Context c) {
        JSONArray w = new JSONArray();
        if (c.getSystemService(AudioManager.class).getStreamVolume(AudioManager.STREAM_ALARM) == 0) w.put("volume_0");
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm.getCurrentInterruptionFilter() == NotificationManager.INTERRUPTION_FILTER_NONE) w.put("total_silence");
        if (!nm.areNotificationsEnabled()) w.put("notifications_off");
        else if (Notes.off(c, App.ALARMS)) w.put("alarm_channel_off");
        if (!nm.canUseFullScreenIntent()) w.put("no_full_screen");
        if (!c.getSystemService(AlarmManager.class).canScheduleExactAlarms()) w.put("no_exact_alarms");
        return w;
    }

    // --- by the owner, on the phone --------------------------------------------------------------------

    static void delete(Context c, String id, String code) {
        Alarm gone;
        try {
            gone = AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                if (a != null) {
                    s.alarms.remove(a);
                    s.event(code, a);
                }
                return a;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "delete", e);
            Notes.change(c, "failed-" + id, "That alarm could not be deleted", "Try again in the app.", null, null);
            return;
        }
        if (gone != null) {
            disarm(c, gone);
            if (gone.ringing) RingService.silence(c, id);
        }
        Notes.clearChange(c, id);
    }

    static void undoCancel(Context c, String id) {
        String text = null;
        try {
            text = AlarmStore.change(c, s -> {
                Alarm found = null;
                for (Alarm a : s.trash) if (a.id.equals(id)) found = a;
                if (s.find(id) != null) return "";  // back already
                if (found == null) return null;
                long now = System.currentTimeMillis();
                if (found.wallClock()) found.toNext(now);
                else found.recompute(c, now);
                if (found.at <= now) return null;
                s.trash.remove(found);
                arm(c, found);
                s.alarms.add(found);
                s.event("restored", found);
                return "";
            });
        } catch (IOException | AlarmStore.Refused | RuntimeException e) {
            Log.e("Ampi", "undo", e);
        }
        Notes.clearChange(c, id);
        if (text == null) {
            Notes.change(c, "failed-" + id, "That alarm could not be restored", "Its time has passed, or it is too long ago. Set it again.", null, null);
        }
    }

    /** A ringing alarm ended: stopped, snoozed, or nobody came. */
    static void finished(Context c, String id, String code, boolean snooze) {
        try {
            AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                // (Not marked as ringing: the mark couldn't be written. Due is enough.)
                if (a == null || (!a.ringing && a.at > System.currentTimeMillis() + EARLY)) {
                    if (snooze) throw new IOException("not in the file");
                    return null;
                }
                end(c, s, a, code);
                if (snooze) {
                    Alarm z = new Alarm();
                    z.id = newId(s);
                    z.kind = a.kind;
                    z.label = a.label;
                    z.created = a.created;  // not newly set: cancelling it still asks the owner
                    z.in(c, System.currentTimeMillis(), SNOOZE);
                    z.snoozed = true;
                    arm(c, z);
                    s.alarms.add(z);
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused | RuntimeException e) {
            Log.e("Ampi", "finished", e);
            if (snooze) Notes.change(c, "failed-" + id, "Snooze didn't work", "Set the alarm again.", null, null);
        }
    }

    static void test(Context c) {
        try {
            AlarmStore.change(c, s -> {
                Alarm a = new Alarm();
                a.id = newId(s);
                a.kind = "timer";
                a.label = "Test";
                a.created = System.currentTimeMillis();
                a.in(c, a.created, 10_000);
                arm(c, a);
                s.alarms.add(a);
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "test", e);
        }
    }

    // --- the clock -------------------------------------------------------------------------------------

    /** Android says alarm {@code id} is due; it was armed for {@code at}. Rings also when the file can't be trusted. */
    static void fire(Context c, String id, long at, String text) {
        long now = System.currentTimeMillis();
        boolean[] ring = {false};
        boolean fresh = at > 0 && now - at <= LATE && at <= now + EARLY;
        try {
            AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                if (a == null) {
                    ring[0] = s.broken && fresh;
                } else {
                    if (!a.wallClock() && !a.ringing) a.recompute(c, now);  // the clock may have jumped just now
                    else if (fresh && at > a.at) a.at = at;  // the file missed the last change
                    ring[0] = due(c, s, a, now);
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused | RuntimeException e) {
            Log.e("Ampi", "fire", e);
            ring[0] |= fresh;  // rather ring without knowing why than stay silent
        }
        if (ring[0]) RingService.ring(c, id, text);
    }

    /**
     * Everything is set again from the file: after a reboot, an update, a change of the time or zone, and every 15 minutes
     * (so an alarm whose ringing ended without a word, e.g. the app was stopped, rings again or is finished). It never rings
     * by itself: only Android's alarm clock may always start the ringing, so a due alarm is handed to it (it fires at once).
     */
    static void rearmAll(Context c) {
        try {
            AlarmStore.change(c, s -> {
                long now = System.currentTimeMillis();
                for (Alarm a : new ArrayList<>(s.alarms)) {
                    if (a.ringing && RingService.isRinging(a.id)) continue;
                    try {
                        if (!a.ringing) a.recompute(c, now);
                        if (a.at <= now + EARLY && now - a.at <= LATE) arm(c, a);
                        else due(c, s, a, now);
                    } catch (RuntimeException e) {  // one alarm's trouble must not keep the others from being set
                        Log.e("Ampi", "rearm " + a.id, e);
                        Notes.change(c, "failed-" + a.id, "An alarm could not be set", a.label, null, null);
                    }
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused | RuntimeException e) {
            Log.e("Ampi", "rearm", e);
        }
    }

    /** Was ringing when the app was killed, and isn't too late to go on. */
    static List<Alarm> cutOff(Context c) {
        long now = System.currentTimeMillis();
        List<Alarm> out = new ArrayList<>();
        for (Alarm a : AlarmStore.get(c).alarms) {
            if (a.ringing && a.at <= now && now - a.at <= LATE && !RingService.isRinging(a.id)) out.add(a);
        }
        return out;
    }

    /** True: ring now. Otherwise the alarm is set again, or over (missed). */
    private static boolean due(Context c, AlarmStore s, Alarm a, long now) throws JSONException {
        if (a.at > now + EARLY) {
            a.ringing = false;
            arm(c, a);
            return false;
        }
        if (now - a.at <= LATE) {
            if (!a.ringing) s.event("rang", a);
            a.ringing = true;
            return true;
        }
        Notes.change(c, "missed-" + a.id, "Missed alarm", a.text(), null, null);
        end(c, s, a, a.ringing ? "stopped_by_android" : "missed_off");
        return false;
    }

    /** One-offs go away; repeating ones are set for their next time. */
    private static void end(Context c, AlarmStore s, Alarm a, String code) throws JSONException {
        s.event(code, a);
        a.ringing = false;
        if (a.repeats()) {
            a.toNext(System.currentTimeMillis());
            arm(c, a);
        } else {
            s.alarms.remove(a);
            disarm(c, a);
        }
    }

    private static void arm(Context c, Alarm a) {
        PendingIntent show = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        c.getSystemService(AlarmManager.class).setAlarmClock(new AlarmManager.AlarmClockInfo(a.at, show), action(c, FIRE, a));
    }

    private static void disarm(Context c, Alarm a) {
        c.getSystemService(AlarmManager.class).cancel(action(c, FIRE, a));
    }

    private static PendingIntent action(Context c, String what, Alarm a) {
        Intent i = new Intent(c, AlarmReceiver.class).setAction(what).setData(Uri.fromParts("ampi", a.id, null))
                .putExtra(AT, a.at).putExtra(TEXT, a.text());
        return PendingIntent.getBroadcast(c, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private static String newId(AlarmStore s) {
        while (true) {
            String id = String.format("%08x", RANDOM.nextInt());
            if (s.find(id) == null) return id;
        }
    }

    // --- checking what the server sent -----------------------------------------------------------------

    static Alarm parse(JSONObject body) throws AlarmStore.Refused {
        Alarm a = new Alarm();
        a.label = Texts.line(body.optString("label"), 60);
        try {
            a.time = LocalTime.parse(body.getString("time")).withSecond(0).withNano(0).toString();
            JSONArray days = body.optJSONArray("days");
            for (int i = 0; days != null && i < days.length(); i++) a.days.add(Alarm.day(days.getString(i)));
            if (body.has("date")) {
                LocalDate d = LocalDate.parse(body.getString("date")), today = LocalDate.now();
                if (d.isBefore(today.minusDays(1)) || d.isAfter(today.plusDays(367))) throw new AlarmStore.Refused("bad_date");
                a.date = d.toString();
            }
        } catch (JSONException | DateTimeParseException e) {
            throw new AlarmStore.Refused("bad_time");
        }
        // Exactly one of them, so that nothing is guessed here.
        if (a.date.isEmpty() == a.days.isEmpty()) throw new AlarmStore.Refused("bad_time");
        return a;
    }
}
