package app.ampi.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;
import android.util.AtomicFile;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;

/** Notifications and the list of messages. Tapping one only ever opens the app. */
final class Notes {
    private static final int MESSAGE = 10, CHANGE = 11, BACKUP = 12, SUMMARY = 13;
    private static final int KEEP_SHOWN = 15, KEEP_CHANGES = 25, KEEP = 50, PER_HOUR = 30;

    private Notes() {}

    record Message(String id, long ts, String title, String text) {}

    /** A message from Ampi; the same id is shown once. */
    static void message(Context c, String id, String title, String text) throws AlarmStore.Refused, IOException, JSONException {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        synchronized (Notes.class) {
            List<Message> all = messages(c);
            for (Message m : all) {  // the same request again: shown before, or only written down
                if (m.id.equals(id) && Arrays.stream(nm.getActiveNotifications()).anyMatch(n -> id.equals(n.getTag()))) return;
            }
            all.removeIf(m -> m.id.equals(id));
            if (!nm.areNotificationsEnabled() || off(c, App.AMPI)) throw new AlarmStore.Refused("notifications_off");
            long now = System.currentTimeMillis();
            if (all.stream().filter(m -> m.ts > now - 3600_000L && m.ts <= now).count() >= PER_HOUR) {
                throw new AlarmStore.Refused("limit_hour");
            }
            all.add(new Message(id, now, title, text));
            while (all.size() > KEEP) all.remove(0);
            save(c, all);
            trim(nm, MESSAGE, KEEP_SHOWN);
            post(c, App.AMPI, id, MESSAGE, title, text, null, null, R.drawable.ic_ampi);
        }
        if (appeared(nm, id)) return;  // not under the lock: the app's screen also needs it
        synchronized (Notes.class) {
            List<Message> all = messages(c);
            all.removeIf(m -> m.id.equals(id));
            save(c, all);
        }
        nm.cancel(id, MESSAGE);
        throw new AlarmStore.Refused("not_shown");
    }

    /** Android shows it a moment later, or (too many, switched off meanwhile) not at all. Not on the main thread. */
    private static boolean appeared(NotificationManager nm, String tag) {
        for (int i = 0; i < 30; i++) {
            if (Arrays.stream(nm.getActiveNotifications()).anyMatch(n -> tag.equals(n.getTag()))) return true;
            SystemClock.sleep(100);
        }
        return false;
    }

    /** Something about alarms the owner should see (what Ampi changed, what was missed). */
    static void change(Context c, String id, String title, String text, String actionName, PendingIntent action) {
        try {
            trim(c.getSystemService(NotificationManager.class), CHANGE, KEEP_CHANGES);
            post(c, App.CHANGES, id, CHANGE, title, text, actionName, action, R.drawable.ic_alarm);
        } catch (RuntimeException e) {
            Log.e("Ampi", "notice", e);  // what it is about has happened anyway
        }
    }

    static void clearChange(Context c, String id) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        nm.cancel(id, CHANGE);
        tidy(nm, CHANGE);
    }

    static void backupAlarm(Context c, String id, String text) {
        post(c, App.BACKUP, id, BACKUP, "Alarm", text, null, null, R.drawable.ic_alarm);
    }

    static boolean off(Context c, String channel) {
        NotificationChannel ch = c.getSystemService(NotificationManager.class).getNotificationChannel(channel);
        return ch != null && ch.getImportance() == NotificationManager.IMPORTANCE_NONE;
    }

    private static void post(Context c, String channel, String tag, int id, String title, String text, String actionName,
                             PendingIntent action, int icon) {
        PendingIntent open = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(c, channel).setSmallIcon(icon).setContentTitle(title)
                .setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text)).setContentIntent(open)
                .setAutoCancel(true).setVisibility(Notification.VISIBILITY_PRIVATE).setShowWhen(true)
                .setAllowSystemGeneratedContextualActions(false);
        if (channel.equals(App.AMPI)) b.setSubText("from Ampi");
        if (!channel.equals(App.BACKUP)) grouped(c, channel, b, icon);
        if (action != null) b.addAction(new Notification.Action.Builder(null, actionName, action).setAuthenticationRequired(true).build());
        c.getSystemService(NotificationManager.class).notify(tag, id, b.build());
    }

    /**
     * Messages and changes get groups of their own. Otherwise Android 16 bundles everything of this app, also the ringing
     * alarm, and a bundled alarm doesn't open its screen over the lock screen.
     */
    private static void grouped(Context c, String channel, Notification.Builder b, int icon) {
        b.setGroup(channel).setGroupAlertBehavior(Notification.GROUP_ALERT_CHILDREN);
        Notification summary = new Notification.Builder(c, channel).setSmallIcon(icon).setGroup(channel).setGroupSummary(true)
                .setGroupAlertBehavior(Notification.GROUP_ALERT_CHILDREN).setAutoCancel(true)
                .setVisibility(Notification.VISIBILITY_PRIVATE).setContentIntent(PendingIntent.getActivity(c, 0,
                        new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE)).build();
        c.getSystemService(NotificationManager.class).notify(channel, SUMMARY, summary);
    }

    /** A group's summary goes when nothing is left in the group. */
    private static void tidy(NotificationManager nm, int gone) {
        boolean left = false;
        for (StatusBarNotification n : nm.getActiveNotifications()) left |= n.getId() == gone;
        if (!left) nm.cancel(gone == MESSAGE ? App.AMPI : App.CHANGES, SUMMARY);
    }

    /** Android drops new notifications silently above 50 per app. Messages never push out notices about alarms. */
    private static void trim(NotificationManager nm, int kind, int keep) {
        List<StatusBarNotification> ours = new ArrayList<>();
        for (StatusBarNotification n : nm.getActiveNotifications()) {
            if (n.getId() == kind) ours.add(n);
        }
        ours.sort(Comparator.comparingLong(StatusBarNotification::getPostTime));
        while (ours.size() >= keep) {
            StatusBarNotification old = ours.remove(0);
            nm.cancel(old.getTag(), old.getId());
        }
    }

    // --- the list of messages (normal storage) -----------------------------------------------------------

    private static AtomicFile file(Context c) {
        return new AtomicFile(new File(c.getFilesDir(), "messages.json"));
    }

    static synchronized List<Message> messages(Context c) {
        List<Message> out = new ArrayList<>();
        AtomicFile f = file(c);
        if (!f.getBaseFile().exists()) return out;
        try {
            JSONArray a = new JSONArray(new String(f.readFully(), StandardCharsets.UTF_8));
            for (int i = 0; i < a.length(); i++) {
                JSONObject o = a.getJSONObject(i);
                out.add(new Message(o.getString("id"), o.getLong("ts"), o.getString("title"), o.getString("text")));
            }
        } catch (IOException | JSONException | RuntimeException e) {
            Log.w("Ampi", "messages", e);
        }
        return out;
    }

    private static void save(Context c, List<Message> all) throws IOException, JSONException {
        JSONArray a = new JSONArray();
        for (Message m : all) a.put(new JSONObject().put("id", m.id).put("ts", m.ts).put("title", m.title).put("text", m.text));
        AlarmStore.save(file(c), a.toString().getBytes(StandardCharsets.UTF_8));
    }

    static synchronized void clear(Context c) {
        file(c).delete();
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        for (StatusBarNotification n : nm.getActiveNotifications()) if (n.getId() == MESSAGE) nm.cancel(n.getTag(), MESSAGE);
        nm.cancel(App.AMPI, SUMMARY);
    }
}
