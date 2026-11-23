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
    static final long LATE = 10 * 60_000L, SNOOZE = 10 * 60_000L, DAY = 24 * 3600_000L;
    static final String FIRE = "app.ampi.companion.FIRE", UNDO_SET = "app.ampi.companion.UNDO_SET",
            UNDO_CANCEL = "app.ampi.companion.UNDO_CANCEL";
    private static final SecureRandom RANDOM = new SecureRandom();

    private Alarms() {}

    // --- asked for by Ampi -----------------------------------------------------------------------------

    /** body: time + (date | days), or at (a timer's moment on this phone's clock). */
    static JSONObject set(Context c, String request, Alarm wanted) throws IOException, AlarmStore.Refused, JSONException {
        long now = System.currentTimeMillis();
        boolean[] existed = {false};
        Alarm done = AlarmStore.change(c, s -> {
            if (s.requests.contains(request)) {  // the same request again: the answer comes from what is stored
                for (Alarm a : s.alarms) if (a.request.equals(request)) return a;
                throw new AlarmStore.Refused("it was set before and is gone again (see the list)");
            }
            for (Alarm a : s.alarms) {
                if (wanted.wallClock() && !a.snoozed && a.identity().equals(wanted.identity())) {
                    existed[0] = true;
                    return a;
                }
            }
            s.sets.removeIf(t -> t <= now - DAY || t > now + DAY);
            if (s.sets.size() >= MAX_PER_DAY) throw new AlarmStore.Refused(MAX_PER_DAY + " alarms a day is the limit");
            if (s.alarms.size() >= MAX) throw new AlarmStore.Refused(MAX + " alarms is the limit");
            wanted.id = newId(s);
            wanted.created = now;
            wanted.request = request;
            if (wanted.wallClock()) wanted.at = wanted.next(now, ZoneId.systemDefault());
            if (wanted.at <= now) throw new AlarmStore.Refused("that time has passed");
            arm(c, wanted);  // first: if Android refuses, nothing is stored as set
            s.alarms.add(wanted);
            s.sets.add(now);
            s.request(request);
            s.event("set", wanted);
            return wanted;
        });
        if (!existed[0] && done == wanted) {
            Notes.change(c, done.id, "Ampi set " + (done.kind.equals("timer") ? "a timer" : "an alarm"), done.text(),
                    "Undo", action(c, UNDO_SET, done.id));
        }
        return new JSONObject().put("id", done.id).put("at", done.at).put("zone", ZoneId.systemDefault().getId())
                .put("existed", existed[0]).put("warn", warnings(c));
    }

    static void cancel(Context c, String request, String id) throws IOException, AlarmStore.Refused {
        Alarm gone = AlarmStore.change(c, s -> {
            Alarm a = s.find(id);
            if (a == null) {
                if (s.requests.contains(request)) return null;  // the same request again
                throw new AlarmStore.Refused("there is no such alarm");
            }
            if (a.ringing) throw new AlarmStore.Refused("it is ringing now; it can be stopped on the phone");
            s.alarms.remove(a);
            s.trash(a);
            s.request(request);
            s.event("cancelled by Ampi", a);
            return a;
        });
        if (gone == null) return;
        disarm(c, id);
        Notes.change(c, id, "Ampi cancelled " + (gone.kind.equals("timer") ? "a timer" : "an alarm"), gone.text(),
                "Undo", action(c, UNDO_CANCEL, id));
    }

    static JSONObject list(Context c) throws JSONException {
        AlarmStore s = AlarmStore.get(c);
        JSONArray rows = new JSONArray();
        for (Alarm a : sorted(s.alarms)) {
            JSONObject o = a.json();
            o.remove("request");
            rows.put(o);
        }
        return new JSONObject().put("alarms", rows).put("events", new JSONArray(s.events))
                .put("zone", ZoneId.systemDefault().getId()).put("warn", warnings(c));
    }

    static List<Alarm> sorted(List<Alarm> in) {
        List<Alarm> out = new ArrayList<>(in);
        out.sort((x, y) -> Long.compare(x.at, y.at));
        return out;
    }

    /** What would keep an alarm from being noticed right now. */
    static JSONArray warnings(Context c) {
        JSONArray w = new JSONArray();
        if (c.getSystemService(AudioManager.class).getStreamVolume(AudioManager.STREAM_ALARM) == 0) w.put("the alarm volume is 0");
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm.getCurrentInterruptionFilter() == NotificationManager.INTERRUPTION_FILTER_NONE) w.put("Do Not Disturb is on total silence");
        if (!nm.areNotificationsEnabled()) w.put("notifications are off for Ampi on the phone");
        else if (Notes.off(c, App.ALARMS)) w.put("the alarm notification is switched off on the phone");
        if (!nm.canUseFullScreenIntent()) w.put("the alarm screen may not open over the lock screen (no permission)");
        if (!c.getSystemService(AlarmManager.class).canScheduleExactAlarms()) w.put("Android doesn't let Ampi set exact alarms");
        return w;
    }

    // --- by the owner, on the phone --------------------------------------------------------------------

    static void delete(Context c, String id, String how) {
        try {
            AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                if (a != null) {
                    s.alarms.remove(a);
                    s.event(how, a);
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "delete", e);
            return;
        }
        disarm(c, id);
        Notes.clearChange(c, id);
    }

    static void undoCancel(Context c, String id) {
        try {
            AlarmStore.change(c, s -> {
                Alarm found = null;
                for (Alarm a : s.trash) if (a.id.equals(id)) found = a;
                if (found == null || s.find(id) != null) return null;
                long now = System.currentTimeMillis();
                if (found.wallClock()) found.at = found.next(now, ZoneId.systemDefault());
                s.trash.remove(found);
                if (found.at <= now) return null;
                arm(c, found);
                s.alarms.add(found);
                s.event("cancelling was undone", found);
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "undo", e);
            return;
        }
        Notes.clearChange(c, id);
    }

    /** A ringing alarm ended: stopped, snoozed, or nobody came. */
    static void finished(Context c, String id, String how, boolean snooze) {
        try {
            AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                if (a == null || !a.ringing) return null;
                end(c, s, a, how);
                if (snooze) {
                    Alarm z = new Alarm();
                    z.id = newId(s);
                    z.kind = a.kind;
                    z.label = a.label;
                    z.created = System.currentTimeMillis();
                    z.at = z.created + SNOOZE;
                    z.snoozed = true;
                    arm(c, z);
                    s.alarms.add(z);
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "finished", e);
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
                a.at = a.created + 10_000;
                arm(c, a);
                s.alarms.add(a);
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "test", e);
        }
    }

    // --- the clock -------------------------------------------------------------------------------------

    /** Android says alarm {@code id} is due. */
    static void fire(Context c, String id) {
        boolean[] ring = {false};
        try {
            AlarmStore.change(c, s -> {
                Alarm a = s.find(id);
                if (a != null) ring[0] = due(c, s, a, System.currentTimeMillis());
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "fire", e);
            ring[0] = true;  // rather ring without knowing why than stay silent
        }
        if (ring[0]) RingService.ring(c, id);
    }

    /** After a reboot, an update, or a change of the time or zone: everything is set again from the file. */
    static void rearmAll(Context c) {
        List<String> ring = new ArrayList<>();
        try {
            AlarmStore.change(c, s -> {
                long now = System.currentTimeMillis();
                for (Alarm a : new ArrayList<>(s.alarms)) {
                    if (a.ringing && RingService.isRinging(a.id)) continue;
                    if (a.wallClock() && a.at > now && !a.ringing) {
                        long next = a.next(now, ZoneId.systemDefault());
                        // A one-off whose date and time are now in the past (the zone changed): late, below.
                        a.at = next > 0 ? next : AlarmTimes.next(LocalTime.parse(a.time), LocalDate.parse(a.date), null, ZoneId.systemDefault(), 0);
                    }
                    if (due(c, s, a, now)) ring.add(a.id);
                }
                return null;
            });
        } catch (IOException | AlarmStore.Refused e) {
            Log.e("Ampi", "rearm", e);
        }
        for (String id : ring) RingService.ring(c, id);
    }

    /** True: ring now. Otherwise the alarm is set again, or over (missed). */
    private static boolean due(Context c, AlarmStore s, Alarm a, long now) throws JSONException {
        if (a.at > now + 1500) {
            arm(c, a);
            return false;
        }
        if (now - a.at <= LATE) {
            if (!a.ringing) s.event("rang", a);
            a.ringing = true;
            return true;
        }
        Notes.change(c, "missed-" + a.id, "Missed alarm", a.text(), null, null);
        end(c, s, a, "missed (the phone was off or the time changed)");
        return false;
    }

    /** One-offs go away; repeating ones are set for their next time. */
    private static void end(Context c, AlarmStore s, Alarm a, String how) throws JSONException {
        s.event(how, a);
        a.ringing = false;
        if (a.repeats()) {
            a.at = a.next(System.currentTimeMillis(), ZoneId.systemDefault());
            arm(c, a);
        } else {
            s.alarms.remove(a);
        }
    }

    private static void arm(Context c, Alarm a) {
        PendingIntent show = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        c.getSystemService(AlarmManager.class).setAlarmClock(new AlarmManager.AlarmClockInfo(a.at, show), action(c, FIRE, a.id));
    }

    private static void disarm(Context c, String id) {
        c.getSystemService(AlarmManager.class).cancel(action(c, FIRE, id));
    }

    private static PendingIntent action(Context c, String what, String id) {
        Intent i = new Intent(c, AlarmReceiver.class).setAction(what).setData(Uri.fromParts("ampi", id, null));
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
                if (d.isBefore(today.minusDays(1)) || d.isAfter(today.plusDays(367))) throw new AlarmStore.Refused("the date must be within a year");
                a.date = d.toString();
            }
        } catch (JSONException | DateTimeParseException e) {
            throw new AlarmStore.Refused("the time, date or days aren't valid");
        }
        // Exactly one of them, so that nothing is guessed here.
        if (a.date.isEmpty() == a.days.isEmpty()) throw new AlarmStore.Refused("a date or days");
        return a;
    }
}
